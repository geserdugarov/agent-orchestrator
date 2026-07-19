# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Resolving-conflict stage handler and its rebase-loop primitives.

`_handle_resolving_conflict` drives an unmergeable PR back to mergeable by
rebasing the per-issue branch onto `<remote>/<base>`. The per-tick handles
(`gh`, `spec`, `issue`, `state`) are bundled into a frozen `_ConflictContext`
so the rebase-loop helpers thread them as a single value (mirrors fixing's
`_FixingContext`); three more frozen records carry step outcomes -- a
`_WorktreeSync` (the worktree measured ahead / behind its remote branch), a
`_DivergeDecision` (the diverged-worktree guard's park-or-publish verdict),
and a `_ConflictResumeRun` (the outputs of one locked dev resume).

The entry point owns the missing-`pr_number` park, the shared PR/issue
terminals, and the user-content-drift resume, then hands to
`_drive_conflict_rebase` (awaiting-human resume, conflict cap),
`_prepare_conflict_worktree` (worktree restore, authenticated branch / base
fetches, ahead/behind classification, diverged-worktree guard, crash-recovery
push), and `_rebase_and_dispose` (base rebase + `merge_attempt` emit routed to
`_publish_clean_rebase` on a clean rebase or `_resolve_conflicts_with_agent`
on real content conflicts). Every pushed-diff exit funnels through
`_hand_resolved_round_to_validating`, which bumps `conflict_round`, emits the
`conflict_round` audit event via `_emit_conflict_round_incremented`, and hands
straight back to `validating`; a no-op base rebase takes the sibling
`_flip_base_up_to_date` (round bumped, no `last_conflict_resolved_at`). Every
park routes through `_park_conflict`. The agent and awaiting-human resumes
share `_run_conflict_resume` and the post-agent disposition funnel
`_post_conflict_resolution_result`, split into the interrupt / timeout /
rebase-in-progress parking probe (`_park_stalled_conflict_result`) and the
push-and-flip finalizer (`_finalize_conflict_resolution`). The diverged-
worktree publish guard (`_guard_diverged_worktree`) keeps its
`_pr_head_orchestrator_produced` / `_already_rebased_onto_base` probes.

ALL workflow-owned helpers (`_park_awaiting_human`, `_now_iso`, the worktree
plumbing, the drift / messaging helpers re-exported into `workflow`, the
validating-side `_post_user_content_change_result`, the implementing-side
`_resume_dev_with_text` / `_on_question` / `_on_dirty_worktree`) are reached
through the parent module via `from orchestrator import workflow as _wf` at call time.
The compatibility surface tests rely on -- `patch.object(workflow, "_foo")` --
has to keep working from inside the stage module too, so the handler must NOT
direct-import these names from `workflow_drift` / `workflow_messages` /
`worktrees`; doing so would bind a stable reference that test patches against
`workflow.X` could not affect.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from github.Issue import Issue

from orchestrator import config
from orchestrator.agents import AgentResult
from orchestrator.comment_trust import filter_trusted
from orchestrator.state_machine import WorkflowLabel
from orchestrator.github import (
    GitHubClient,
    PinnedState,
)


# Pinned-state round counters this stage reads and writes.
_CONFLICT_ROUND = "conflict_round"
_REVIEW_ROUND = "review_round"


@dataclass(frozen=True)
class _ConflictContext:
    """The per-tick `resolving_conflict` handles, bundled so the rebase-loop
    helpers thread them as a single value instead of four positional
    arguments (mirrors fixing's `_FixingContext`)."""
    gh: GitHubClient
    spec: config.RepoSpec
    issue: Issue
    state: PinnedState


@dataclass(frozen=True)
class _WorktreeSync:
    """A PR worktree measured against its remote branch tip: the worktree
    path, the branch name, and how far HEAD is ahead / behind the freshly
    fetched `<remote>/<branch>` head."""
    worktree: Path
    branch: str
    ahead: int
    behind: int


@dataclass(frozen=True)
class _DivergeDecision:
    """Verdict of the diverged-worktree guard: whether the tick parked, plus
    the force-publish lease pinned to a validated orchestrator-produced PR
    head when an already-rebased worktree may be force-published instead."""
    parked: bool
    publish_lease: Optional[str] = None


@dataclass(frozen=True)
class _ConflictResumeRun:
    """The outputs of one locked dev resume in the rebase loop: the worktree
    the agent ran in (`_resume_dev_with_text` may re-create it), the agent
    result, and whether an operator paused mid-run."""
    worktree: Path
    dev_result: AgentResult
    paused: bool


def _park_conflict(ctx: _ConflictContext, message: str, *, reason: str) -> None:
    """Park awaiting human and persist pinned state.

    Every `resolving_conflict` park pairs `_park_awaiting_human` with the
    matching `write_pinned_state`; routing them through here keeps the two
    from drifting apart across the handler's many exits.
    """
    from orchestrator import workflow as _wf

    _wf._park_awaiting_human(ctx.gh, ctx.issue, ctx.state, message, reason=reason)
    ctx.gh.write_pinned_state(ctx.issue, ctx.state)


def _emit_conflict_round_incremented(
    ctx: _ConflictContext,
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
    ctx.gh.emit_event(
        _CONFLICT_ROUND,
        issue_number=ctx.issue.number,
        stage="resolving_conflict",
        pr_number=int(pr_number),
        sha=sha or None,
        action="incremented",
        conflict_round=int(new_round),
        outcome=outcome,
        review_round=int(ctx.state.get(_REVIEW_ROUND) or 0),
        retry_count=ctx.state.get("retry_count"),
    )


def _hand_resolved_round_to_validating(
    ctx: _ConflictContext,
    conflict_round: int,
    pr_number,
    *,
    outcome: str,
    sha: Optional[str],
) -> None:
    """Record a pushed conflict-resolution round and hand back to `validating`.

    Resets `review_round` (rebasing rewrites SHAs, so validation must
    re-approve the rebased branch), bumps `conflict_round`, stamps
    `last_conflict_resolved_at`, emits the `conflict_round` audit event, flips
    the label, and persists pinned state. Shared by every pushed-diff exit --
    recovered push, clean base rebase, agent resolution, and the drift resume.
    Docs do not run here: the single docs pass is deferred to the post-approval
    handoff to `documenting` in `_handle_validating`.
    """
    from orchestrator import workflow as _wf

    ctx.state.set(_REVIEW_ROUND, 0)
    ctx.state.set(_CONFLICT_ROUND, conflict_round + 1)
    ctx.state.set("last_conflict_resolved_at", _wf._now_iso())
    _emit_conflict_round_incremented(
        ctx,
        pr_number=int(pr_number),
        new_round=conflict_round + 1,
        outcome=outcome,
        sha=sha,
    )
    ctx.gh.set_workflow_label(ctx.issue, WorkflowLabel.VALIDATING)
    ctx.gh.write_pinned_state(ctx.issue, ctx.state)


def _ensure_conflict_worktree(ctx: _ConflictContext) -> Path:
    """Return the per-issue worktree, restoring it from `origin/<branch>` when
    it has been pruned.

    The PR-aware `_ensure_pr_worktree` (not `_ensure_worktree`) rebuilds from
    the PR branch so the PR's commits survive; `_ensure_worktree` would
    silently rebuild from `origin/<base>` and discard them.
    """
    from orchestrator import workflow as _wf

    wt = _wf._worktree_path(ctx.spec, ctx.issue.number)
    if not wt.exists():
        wt = _wf._ensure_pr_worktree(
            ctx.spec, ctx.issue.number,
            branch=_wf._resolve_branch_name(ctx.state, ctx.spec, ctx.issue.number),
        )
    return wt


def _pr_head_orchestrator_produced(state: PinnedState, pr) -> bool:
    """True when the remote PR head is a SHA the orchestrator itself recorded.

    Guards the force-publish of a diverged-but-already-rebased branch
    (the `behind > 0` exception in `_guard_diverged_worktree`): the
    orchestrator's own prior head -- the SHA `_handle_documenting`
    persists as `docs_checked_sha` on its success exits -- is the one
    case we can prove is safe to overwrite. An unrecognized head may
    carry a commit pushed directly to the PR branch, so a divergence
    from it must stay parked. PR heads from earlier in the lifecycle
    (the initial implementing push, an intermediate fixing push) are
    not currently recorded anywhere in pinned state, so the exception
    declines those by design rather than guessing.
    """
    head = getattr(getattr(pr, "head", None), "sha", None) or ""
    return bool(head) and head == state.get("docs_checked_sha")


def _already_rebased_onto_base(spec: config.RepoSpec, wt: Path) -> bool:
    """True when the worktree HEAD already sits on top of `<remote>/<base>`.

    Re-fetches base first (the ahead/behind check that calls this runs
    BEFORE the handler's own base fetch lower down) and checks that no
    base commit is missing from HEAD. Used to recognize a worktree the
    dev already rebased in an earlier run -- a no-op rebase that only
    needs publishing, not the diverged-branch park.

    Fails closed on fetch failure: a stale `<remote>/<base>` ref would
    let `rev-list HEAD..<remote>/<base>` report "no missing commits"
    purely because the local mirror predates the real base tip, which
    would incorrectly enable the force-publish path without proving HEAD
    is on the current base.
    """
    from orchestrator import workflow as _wf

    fetch = _wf._authed_fetch(
        spec,
        f"+refs/heads/{spec.base_branch}:"
        f"refs/remotes/{spec.remote_name}/{spec.base_branch}",
        cwd=wt,
    )
    if fetch.returncode != 0:
        return False
    base_distance_result = _wf._git_hardened(
        "rev-list", "--count",
        f"HEAD..{spec.remote_name}/{spec.base_branch}", cwd=wt,
    )
    if base_distance_result.returncode != 0:
        return False
    try:
        return int((base_distance_result.stdout or "").strip() or 0) == 0
    except ValueError:
        return False


def _handle_resolving_conflict(
    gh: GitHubClient, spec: config.RepoSpec, issue: Issue
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
    from orchestrator import workflow as _wf

    state = gh.read_pinned_state(issue)
    ctx = _ConflictContext(gh, spec, issue, state)
    pr_number = state.get("pr_number")

    if pr_number is None:
        _park_conflict_missing_pr_number(ctx)
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
        _resume_on_user_content_change(ctx, pr_number, new_hash)
        return

    _drive_conflict_rebase(ctx, pr, pr_number)


def _park_conflict_missing_pr_number(ctx: _ConflictContext) -> None:
    """Park a `resolving_conflict` issue that carries no pinned `pr_number`.

    Reaching here means a manual relabel from outside the normal route; the
    rebase / push paths all need the PR. An already-parked issue is left alone
    so the park comment is not re-posted every tick.
    """
    if ctx.state.get("awaiting_human"):
        return
    _park_conflict(
        ctx,
        f"{config.HITL_MENTIONS} `resolving_conflict` without a pinned "
        "`pr_number`; manual relabeling suspected. Set the workflow "
        "label back to `validating` after fixing.",
        reason="missing_pr_number",
    )


def _drive_conflict_rebase(ctx: _ConflictContext, pr, pr_number) -> None:
    """Route past the awaiting-human resume and the conflict cap, then prepare
    the worktree and rebase.

    Resume-on-human-reply comes first: when parked awaiting human and a new
    comment arrived, resume the dev session on the in-progress rebase worktree
    with the human's text (mirrors `_handle_implementing`'s awaiting-human
    path so a `_on_question` / `_on_dirty_worktree` park can be unstuck by a
    comment, as the park messages invite). The cap parks awaiting human once
    `MAX_CONFLICT_ROUNDS` rounds have failed.
    """
    conflict_round = int(ctx.state.get(_CONFLICT_ROUND) or 0)

    if ctx.state.get("awaiting_human"):
        _resume_awaiting_human(ctx, conflict_round)
        return

    if conflict_round >= config.MAX_CONFLICT_ROUNDS:
        _park_conflict(
            ctx,
            f"{config.HITL_MENTIONS} auto-conflict-resolution still failing "
            f"after {conflict_round} round(s) "
            f"(`MAX_CONFLICT_ROUNDS={config.MAX_CONFLICT_ROUNDS}`); manual "
            "intervention needed.",
            reason="conflict_cap",
        )
        return

    wt = _prepare_conflict_worktree(ctx, pr, pr_number, conflict_round)
    if wt is None:
        return

    _rebase_and_dispose(ctx, pr_number, conflict_round, wt)


def _prepare_conflict_worktree(
    ctx: _ConflictContext, pr, pr_number, conflict_round: int,
) -> Optional[Path]:
    """Restore the worktree, refresh remote refs, and reconcile a diverged or
    crash-recovered branch before the base rebase.

    Returns the worktree to rebase, or ``None`` when the tick is fully handled
    (a fetch failure / diverged-branch / dirty park, or a crash-recovery push
    that flipped straight to `validating`) and the caller must return.
    """
    from orchestrator import workflow as _wf

    wt = _ensure_conflict_worktree(ctx)
    branch = _wf._resolve_branch_name(ctx.state, ctx.spec, ctx.issue.number)

    # Refresh `<remote>/<branch>` (the PR branch's remote tip) via the same
    # hardened authenticated path `_push_branch` uses. A stale local ref would
    # mis-classify a real "remote moved out from under us" as in-sync.
    if not _fetch_pr_branch(ctx, wt, branch):
        return None

    # Check the worktree against the freshly-fetched remote PR head. Three
    # shapes: in sync `(0, 0)` proceeds to the base rebase; HEAD ahead
    # `(>0, 0)` is the crash-recovery case (a prior tick committed a
    # resolution but crashed before the push / post-push state write landed);
    # anything `behind > 0` is a stale or diverged worktree we refuse to
    # force-push over.
    sync = _WorktreeSync(
        wt, branch, *_wf._branch_ahead_behind(ctx.spec, wt, branch),
    )
    guard = _guard_diverged_worktree(ctx, pr, sync)
    if guard.parked:
        return None
    if sync.ahead > 0 and _push_recovered_commits(
        ctx, sync, conflict_round, pr_number, guard.publish_lease,
    ):
        return None

    # In sync (or fell through after a recovered push to reconcile a stale
    # base). Refresh `<remote>/<base>` so the upcoming rebase sees the current
    # base tip.
    if not _fetch_base_ref(ctx, wt):
        return None
    return wt


def _fetch_pr_branch(ctx: _ConflictContext, wt: Path, branch: str) -> bool:
    """Fetch `<remote>/<branch>` into the worktree. Returns False (after
    parking) on fetch failure, True otherwise."""
    from orchestrator import workflow as _wf

    spec = ctx.spec
    fetch_branch = _wf._authed_fetch(
        spec,
        f"+refs/heads/{branch}:refs/remotes/{spec.remote_name}/{branch}",
        cwd=wt,
    )
    if fetch_branch.returncode == 0:
        return True
    _wf.log.error(
        "issue=#%d branch fetch failed in resolving_conflict: %s",
        ctx.issue.number, (fetch_branch.stderr or "").strip(),
    )
    _park_conflict(
        ctx,
        f"{config.HITL_MENTIONS} `git fetch {spec.remote_name} {branch}` "
        "failed during conflict resolution; see orchestrator logs.",
        reason="fetch_failed",
    )
    return False


def _fetch_base_ref(ctx: _ConflictContext, wt: Path) -> bool:
    """Fetch `<remote>/<base>` into the worktree. Returns False (after
    parking) on fetch failure, True otherwise."""
    from orchestrator import workflow as _wf

    spec = ctx.spec
    fetch_base = _wf._authed_fetch(
        spec,
        f"+refs/heads/{spec.base_branch}:"
        f"refs/remotes/{spec.remote_name}/{spec.base_branch}",
        cwd=wt,
    )
    if fetch_base.returncode == 0:
        return True
    _wf.log.error(
        "issue=#%d base fetch failed in resolving_conflict: %s",
        ctx.issue.number, (fetch_base.stderr or "").strip(),
    )
    _park_conflict(
        ctx,
        f"{config.HITL_MENTIONS} "
        f"`git fetch {spec.remote_name} {spec.base_branch}` "
        "failed during conflict resolution; see orchestrator logs.",
        reason="fetch_failed",
    )
    return False


def _rebase_and_dispose(
    ctx: _ConflictContext, pr_number, conflict_round: int, wt: Path,
) -> None:
    """Rebase the worktree onto base, emit `merge_attempt`, and dispose.

    A clean rebase routes to `_publish_clean_rebase`; a rebase that failed
    without listing conflicted files parks; real content conflicts hand to
    `_resolve_conflicts_with_agent`.
    """
    from orchestrator import workflow as _wf

    spec = ctx.spec
    before_sha = _wf._head_sha(wt)
    succeeded, conflicted_files = _wf._rebase_base_into_worktree(spec, wt)
    ctx.gh.emit_event(
        "merge_attempt",
        issue_number=ctx.issue.number,
        stage="resolving_conflict",
        pr_number=int(pr_number),
        sha=before_sha or None,
        method="base_rebase",
        result=_merge_result(succeeded, conflicted_files),
        conflict_round=conflict_round,
        review_round=int(ctx.state.get(_REVIEW_ROUND) or 0),
        retry_count=ctx.state.get("retry_count"),
    )

    if succeeded:
        _publish_clean_rebase(ctx, wt, before_sha, conflict_round, pr_number)
        return

    if not conflicted_files:
        _park_conflict(
            ctx,
            f"{config.HITL_MENTIONS} "
            f"`git rebase {spec.remote_name}/{spec.base_branch}` "
            "failed without listing conflicted files; manual intervention "
            "needed.",
            reason="rebase_failed_no_files",
        )
        return

    _resolve_conflicts_with_agent(
        ctx, conflicted_files, before_sha, conflict_round,
    )


def _merge_result(succeeded: bool, conflicted_files) -> str:
    """Map a base-rebase outcome to the `merge_attempt` event's `result`."""
    if succeeded:
        return "success"
    return "conflict" if conflicted_files else "failed"


def _resume_on_user_content_change(
    ctx: _ConflictContext,
    pr_number,
    new_hash: str,
) -> None:
    """Resume the dev session after a human edited the issue body mid-rebase.

    Posts a resuming ack, marks the drift comments consumed, and resumes
    the dev on the updated body+comments. On a pushed fix bumps the
    conflict round and hands to `validating`; on an ack (no commit) stays
    in `resolving_conflict` without parking. The caller returns immediately
    after this helper runs. Persists pinned state on every exit EXCEPT the
    shutdown-sweep-interrupted / live-paused short-circuits, which return
    without writing so the drift stays unconsumed and re-runs next process.
    """
    from orchestrator import workflow as _wf

    ctx.state.set("user_content_hash", new_hash)
    _wf._post_pr_comment(
        ctx.gh, int(pr_number), ctx.state,
        ":pencil2: issue body changed; resuming dev session.",
    )
    # Mark issue-thread comments as consumed: the dev sees the full thread via
    # `_recent_comments_text`, and the eventual validating->in_review handoff
    # (after a successful pushed resolution flips back to validating) must not
    # replay them.
    _wf._mark_drift_comments_consumed(ctx.gh, ctx.issue, ctx.state)
    wt = _ensure_conflict_worktree(ctx)
    before_sha = _wf._head_sha(wt)
    followup = _wf._build_user_content_change_prompt(
        ctx.issue, _wf._recent_comments_text(ctx.issue),
    )
    run = _run_conflict_resume(ctx, followup)
    # Shutdown-sweep interruption: ignore the partial result and return WITHOUT
    # writing pinned state -- the drift bookkeeping (refreshed
    # `user_content_hash`, consumed comments, session mutations) above is
    # discarded so the next process re-detects and re-runs the drift resume.
    # Must precede `_post_user_content_change_result`, which has no interrupted
    # check of its own and would otherwise parse `last_message` / route through
    # `_on_question` before the caller persists those changes.
    if _wf._ignore_if_interrupted(ctx.issue, run.dev_result):
        return
    # Live pause applied mid-run: an operator added `paused` (or `backlog`)
    # while this drift resume was in flight. Same short-circuit as the
    # interrupted branch -- return before `_post_user_content_change_result`,
    # the conflict-round bump, or any relabel / pinned-state write, so the
    # drift stays unconsumed and the committed work stays on the branch until
    # the label is removed.
    if run.paused:
        return
    outcome = _wf._post_user_content_change_result(
        ctx.gh, ctx.spec, ctx.issue, ctx.state, run.worktree,
        run.dev_result, before_sha,
    )
    if outcome == "pushed":
        # Pushed branch diff -> hand straight back to validating; the single
        # docs pass runs after final reviewer approval.
        _hand_resolved_round_to_validating(
            ctx, int(ctx.state.get(_CONFLICT_ROUND) or 0), pr_number,
            outcome="drift_resolved", sha=_wf._head_sha(run.worktree),
        )
        return
    ctx.gh.write_pinned_state(ctx.issue, ctx.state)


def _resume_awaiting_human(
    ctx: _ConflictContext, conflict_round: int,
) -> None:
    """Resume a parked rebase on a fresh human reply.

    Collects comments past `last_action_comment_id`, resumes the dev with
    their text, and funnels the result through
    `_post_conflict_resolution_result`. Returns without writing pinned
    state when no reply has arrived yet or a live pause landed mid-run; on
    a real reply the shared funnel owns the push / relabel / state write.
    """
    from orchestrator import workflow as _wf

    followup = _awaiting_human_followup(ctx)
    if followup is None:
        return
    wt = _ensure_conflict_worktree(ctx)
    before_sha = _wf._head_sha(wt)
    run = _run_conflict_resume(ctx, followup)
    # Live pause applied mid-run: honor the helper's decision and return
    # before `_post_conflict_resolution_result` (which parses the result,
    # pushes, relabels, and writes pinned state). The in-progress rebase stays
    # on the branch until the label is removed.
    if run.paused:
        return
    # No explicit lease here: resume worktrees may be mid-rebase or ahead of
    # the remote PR head, so `before_sha` is not necessarily the remote SHA.
    # Let `_push_branch` lease against live ls-remote.
    _post_conflict_resolution_result(ctx, run, before_sha, conflict_round)


def _awaiting_human_followup(ctx: _ConflictContext) -> Optional[str]:
    """Build the dev-resume prompt for a parked rebase from the trusted human
    reply, or return ``None`` when the tick is handled without a resume.

    Returns ``None`` when no trusted reply has arrived yet (no state write) or
    the `/orchestrator continue` command is refused (park written). Otherwise
    advances the consumed-comment watermark and returns the retry prompt or the
    joined reply text.
    """
    from orchestrator import workflow as _wf

    last_action_id = ctx.state.get("last_action_comment_id")
    # Drop untrusted authors up front (mirrors `_resume_developer_on_human_reply`):
    # with `ALLOWED_ISSUE_AUTHORS` set an outsider reply on a parked rebase must
    # not steer the developer NOR advance the consumed watermark. Only trusted
    # comments are consumed, so an outsider reply trailing a trusted one is left
    # unconsumed; an all-untrusted batch is treated as "no human reply yet".
    new_comments = filter_trusted(ctx.gh.comments_after(ctx.issue, last_action_id))
    if not new_comments:
        return None  # no human reply yet
    # `/orchestrator continue` on a parked rebase, BEFORE the generic comment
    # resume. A session-failure park (`agent_silent` / `agent_timeout`) retries
    # the dev intentionally on a neutral prompt -- NOT the literal command,
    # which the dev has no context for -- while a park needing a real answer
    # refuses. Auto-rebase parks belong to the refresh retry-unpark, so leave
    # those (and command-plus-guidance / normal replies) to the resume below.
    park_reason = ctx.state.get("park_reason")
    continue_action = (
        "passthrough" if park_reason in _wf._AUTO_REBASE_PARK_REASONS
        else _wf._continue_command_action(new_comments, park_reason)
    )
    if continue_action == "refuse":
        _wf._refuse_parked_continue(ctx.gh, ctx.issue, ctx.state)
        ctx.gh.write_pinned_state(ctx.issue, ctx.state)
        return None
    ctx.state.set(
        "last_action_comment_id", max(comment.id for comment in new_comments),
    )
    if continue_action == "retry":
        return f"{_wf._CONTINUE_RETRY_PROMPT}\n\n{_wf._FOREGROUND_ONLY_NOTE}"
    joined = "\n\n".join(
        _wf._quote_comment_line(comment)
        for comment in new_comments
        if comment.body
    )
    return f"{joined}\n\n{_wf._FOREGROUND_ONLY_NOTE}"


def _run_conflict_resume(
    ctx: _ConflictContext, followup: str,
) -> _ConflictResumeRun:
    """Resume the locked dev session over `followup` and stamp the agent
    action time. Shared by the drift, awaiting-human, and fresh-conflict
    resume paths."""
    from orchestrator import workflow as _wf

    wt, conflict_result, paused = _wf._resume_dev_with_text(
        ctx.gh, ctx.spec, ctx.issue, ctx.state, followup, pause_guard=True,
    )
    ctx.state.set("last_agent_action_at", _wf._now_iso())
    return _ConflictResumeRun(worktree=wt, dev_result=conflict_result, paused=paused)


def _guard_diverged_worktree(
    ctx: _ConflictContext, pr, sync: _WorktreeSync,
) -> _DivergeDecision:
    """Decide the fate of a worktree behind the remote PR head.

    When `behind > 0` the worktree is normally stale or diverged and we refuse
    the force-push, park, and return a parked decision. The one exception --
    an already-rebased worktree ahead of a stale orchestrator-produced PR head
    -- yields a lease pinned to the validated head so the recovered-push router
    can force-publish it. Every other case (including `behind == 0`) returns an
    unparked decision with no lease.
    """
    from orchestrator import workflow as _wf

    if sync.behind <= 0:
        return _DivergeDecision(parked=False)

    # One exception to the refuse-and-park default: the worktree is already
    # correctly rebased ONTO base, ahead of the PR head, and the "behind"
    # commits are the orchestrator's OWN superseded pre-rebase commits on a
    # head it produced (a rebase a prior run ran but never pushed -- exactly
    # the case the fixing dead-lock router hands us). That is the
    # reconciliation this handler exists for: publish instead of park.
    # `_already_rebased_onto_base` re-fetches base to be sure, and the
    # orchestrator-produced check proves there is no external commit on the PR
    # branch to lose.
    if (
        sync.ahead > 0
        and _pr_head_orchestrator_produced(ctx.state, pr)
        and _already_rebased_onto_base(ctx.spec, sync.worktree)
    ):
        _wf.log.info(
            "issue=#%d resolving_conflict: worktree already rebased onto "
            "%s/%s and ahead of a stale orchestrator-produced PR head "
            "(`%s`); force-publishing instead of parking",
            ctx.issue.number, ctx.spec.remote_name, ctx.spec.base_branch,
            pr.head.sha[:8],
        )
        # Pin the upcoming force-push lease to the exact PR head we just
        # validated as orchestrator-produced. A bare `_push_branch` would do a
        # fresh `ls-remote` and lease against whatever SHA is live at push time
        # -- if a foreign push lands on the PR branch between `gh.get_pr()` and
        # the push below, the new SHA would become the lease and the force-push
        # would silently overwrite it. Leasing against the validated SHA
        # refuses any such concurrent update.
        return _DivergeDecision(parked=False, publish_lease=pr.head.sha)

    _park_diverged_worktree(ctx, pr, sync)
    return _DivergeDecision(parked=True)


def _park_diverged_worktree(
    ctx: _ConflictContext, pr, sync: _WorktreeSync,
) -> None:
    """Park a stale / diverged worktree: force-pushing the local state would
    clobber the real PR head."""
    spec = ctx.spec
    pr_head_short = pr.head.sha[:8]
    _park_conflict(
        ctx,
        f"{config.HITL_MENTIONS} worktree on `{sync.branch}` is {sync.ahead} "
        f"ahead and {sync.behind} behind `{spec.remote_name}/{sync.branch}` "
        f"(PR head `{pr_head_short}`); refusing to rebase a stale "
        "or diverged branch -- force-pushing the local state would "
        "clobber the real PR head. Manual intervention needed.",
        reason="diverged_branch",
    )


def _push_recovered_commits(
    ctx: _ConflictContext,
    sync: _WorktreeSync,
    conflict_round: int,
    pr_number,
    publish_lease: Optional[str],
) -> bool:
    """Push crash-recovered commits ahead of the remote PR head.

    Returns True when the tick is fully handled (caller returns): a dirty
    tree or failed push parks, and a recovered push that leaves HEAD on
    base flips straight to `validating`. Returns False -- continue to the
    base rebase -- when the push landed but the worktree is still behind
    base (the fixing dead-lock reroute lands unpushed fix commits here,
    NOT a rebase, so the combined push+rebase round is owned by the rebase
    path).
    """
    from orchestrator import workflow as _wf

    spec = ctx.spec
    wt = sync.worktree
    # Dirty check before pushing recovered work: if the previous tick crashed
    # before its own dirty check ran, the worktree may carry uncommitted edits
    # the unpushed commit does NOT contain. Pushing in that state would publish
    # a SHA that silently omits those edits, and the reviewer at validating
    # would later run on a local tree that does not match the PR. Mirror
    # `_on_dirty_worktree`: park awaiting human, no flip.
    dirty = _wf._worktree_dirty_files(wt)
    if dirty:
        _park_conflict(
            ctx,
            f"{config.HITL_MENTIONS} worktree has {len(dirty)} "
            "uncommitted change(s) alongside recovered conflict "
            "resolution; refusing to push an incomplete branch. "
            "Resolve the dirty tree manually before resuming.",
            reason="dirty_worktree",
        )
        return True
    _wf.log.info(
        "issue=#%d resolving_conflict: pushing %d recovered commit(s) "
        "ahead of %s/%s before attempting base rebase",
        ctx.issue.number, sync.ahead, spec.remote_name, sync.branch,
    )
    if not _wf._push_branch(
        spec, wt, sync.branch, force_with_lease=publish_lease,
    ):
        _park_conflict(
            ctx,
            f"{config.HITL_MENTIONS} git push of recovered conflict "
            "resolution failed; see orchestrator logs.",
            reason="push_failed",
        )
        return True
    # Probe whether the worktree is still behind base after the push. The
    # recovered-push case was originally written for crash-recovery where the
    # prior tick had already rebased onto base before crashing -- HEAD contains
    # base, the follow-up rebase would be a no-op, and a direct flip to
    # validating is correct. But the `fixing` drift router
    # (`_reconcile_parked_fixing`) also reroutes here when a `push_failed` park
    # has UNPUSHED FIX COMMITS on a stale base: the commits are NOT a rebase, so
    # the push above leaves the branch still behind base. Marking validating now
    # would publish a still-behind PR and consume a `conflict_round` without
    # ever attempting the base rebase -- under a low `MAX_CONFLICT_ROUNDS` the
    # real rebase pass could even be blocked by the cap. When the probe confirms
    # behind base, fall through to the rebase path; that path owns the
    # bookkeeping (conflict_round bump, event emit, label flip) for the combined
    # push+rebase round.
    base_ref = f"{spec.remote_name}/{spec.base_branch}"
    still_behind = _still_behind_base(wt, base_ref)
    if still_behind != 0:
        _wf.log.info(
            "issue=#%d resolving_conflict: pushed %d recovered commit(s) "
            "but worktree still %d behind %s; continuing with base rebase",
            ctx.issue.number, sync.ahead, still_behind, base_ref,
        )
        return False
    # Pushed branch diff -> hand straight back to validating; the single docs
    # pass runs after final reviewer approval.
    _hand_resolved_round_to_validating(
        ctx, conflict_round, pr_number,
        outcome="recovered_push", sha=_wf._head_sha(wt),
    )
    return True


def _still_behind_base(wt: Path, base_ref: str) -> int:
    """Count commits on `base_ref` missing from HEAD, failing closed to 1.

    A probe failure (stale base ref, transient git error) reports "behind" so
    the caller falls through to the rebase path: `_rebase_base_into_worktree`
    no-ops when HEAD already contains base and re-fetches to self-correct a
    stale ref, which is the safer default than a blind fast-path to validating.
    """
    from orchestrator import workflow as _wf

    behind_base_r = _wf._git(
        "rev-list", "--count", f"HEAD..{base_ref}", cwd=wt,
    )
    if behind_base_r.returncode != 0:
        return 1
    try:
        return int((behind_base_r.stdout or "").strip() or 0)
    except ValueError:
        return 1


def _publish_clean_rebase(
    ctx: _ConflictContext,
    wt: Path,
    before_sha: str,
    conflict_round: int,
    pr_number,
) -> None:
    """Dispose of a clean `git rebase <remote>/<base>` outcome.

    Parks on a dirty tree; flips to `validating` without a push when the
    base had not moved (no-op rebase, still counted against the cap); or
    force-pushes the rebased head and flips to `validating`. The caller
    returns immediately after; every exit writes pinned state.
    """
    from orchestrator import workflow as _wf

    spec = ctx.spec
    # Dirty check before EITHER clean-rebase exit (no-op flip OR rebased-head
    # push): a pre-existing uncommitted edit (left by a previous tick that
    # crashed before its own dirty check ran) would otherwise survive a no-op
    # flip into validating, where the reviewer agent reads the worktree
    # directly. The reviewer would then vote on a tree that does NOT match the
    # PR head; the in_review HITL ready-ping would later advertise the PR as
    # ready for human merge with the reviewer's approval sitting against an
    # incorrect SHA, inviting a human merge over unreviewed content. Park
    # rather than push or flip, mirroring `_on_dirty_worktree`'s "refuse to
    # publish an incomplete branch" rule.
    dirty = _wf._worktree_dirty_files(wt)
    if dirty:
        _park_conflict(
            ctx,
            f"{config.HITL_MENTIONS} worktree has {len(dirty)} "
            f"uncommitted change(s) after `git rebase "
            f"{spec.remote_name}/{spec.base_branch}`; refusing to "
            "push or hand back to validating with a dirty tree.",
            reason="dirty_worktree",
        )
        return
    after_sha = _wf._head_sha(wt)
    if not after_sha or after_sha == before_sha:
        _flip_base_up_to_date(ctx, conflict_round, pr_number, after_sha)
        return
    if not _wf._push_branch(
        spec, wt, _wf._resolve_branch_name(ctx.state, spec, ctx.issue.number),
        force_with_lease=before_sha or None,
    ):
        _park_conflict(
            ctx,
            f"{config.HITL_MENTIONS} git push failed after auto-rebasing "
            f"`{spec.remote_name}/{spec.base_branch}`; "
            "see orchestrator logs.",
            reason="push_failed",
        )
        return
    # Pushed branch diff -> hand straight back to validating; the single docs
    # pass runs after final reviewer approval.
    _hand_resolved_round_to_validating(
        ctx, conflict_round, pr_number,
        outcome="base_rebased_clean", sha=after_sha,
    )


def _flip_base_up_to_date(
    ctx: _ConflictContext, conflict_round: int, pr_number, after_sha,
) -> None:
    """Hand a no-op base rebase (branch already current) back to `validating`.

    Increments `conflict_round` even though no diff was applied: an unmergeable
    PR blocked purely by branch protection / required reviewers (PyGithub
    cannot tell those from a content conflict) would otherwise loop
    in_review <-> resolving_conflict forever with the cap never firing.
    Counting the no-op against the cap surfaces it within MAX_CONFLICT_ROUNDS
    ticks. Does NOT stamp `last_conflict_resolved_at` -- nothing was resolved.
    """
    from orchestrator import workflow as _wf

    _wf.log.info(
        "issue=#%d resolving_conflict: branch already up-to-date with %s/%s",
        ctx.issue.number, ctx.spec.remote_name, ctx.spec.base_branch,
    )
    ctx.state.set(_REVIEW_ROUND, 0)
    ctx.state.set(_CONFLICT_ROUND, conflict_round + 1)
    _emit_conflict_round_incremented(
        ctx,
        pr_number=int(pr_number),
        new_round=conflict_round + 1,
        outcome="base_up_to_date",
        sha=after_sha,
    )
    ctx.gh.set_workflow_label(ctx.issue, WorkflowLabel.VALIDATING)
    ctx.gh.write_pinned_state(ctx.issue, ctx.state)


def _resolve_conflicts_with_agent(
    ctx: _ConflictContext,
    conflicted_files,
    before_sha: str,
    conflict_round: int,
) -> None:
    """Resume the dev session to resolve real rebase content conflicts.

    Builds the conflict-resolution prompt from the conflicted files,
    resumes the locked backend, and funnels the result through
    `_post_conflict_resolution_result` (leasing the push against
    `before_sha`). Returns without touching durable state when a live
    pause lands mid-run.
    """
    from orchestrator import workflow as _wf

    spec = ctx.spec
    fix_prompt = _wf._build_conflict_resolution_prompt(
        f"{spec.remote_name}/{spec.base_branch}", conflicted_files,
    )
    run = _run_conflict_resume(ctx, fix_prompt)
    # Live pause applied mid-run: return before
    # `_post_conflict_resolution_result` pushes / relabels / writes pinned
    # state -- the resolved commit stays on the branch until the label is
    # removed.
    if run.paused:
        return
    _post_conflict_resolution_result(
        ctx, run, before_sha, conflict_round,
        force_with_lease=before_sha or None,
    )


def _post_conflict_resolution_result(
    ctx: _ConflictContext,
    run: _ConflictResumeRun,
    before_sha: str,
    conflict_round: int,
    *,
    force_with_lease: Optional[str] = None,
) -> None:
    """Common post-agent handling for both fresh conflict resolution
    and the awaiting-human resume path.

    Calls `gh.write_pinned_state` before returning on every branch EXCEPT
    the shutdown-sweep-interrupted short-circuit (inside
    `_park_stalled_conflict_result`), which returns without writing so
    durable GitHub state stays retryable. The caller returns immediately
    after invoking this helper either way. Increments `conflict_round`
    only on the success path -- failure paths leave the counter alone so a
    human-reply resume that lands cleanly still consumes a slot, but a
    timeout/dirty/push-failure on the same counter does not. A successful
    push hands straight back to `validating` so the reviewer re-runs
    against the resolved branch; the single docs pass is deferred to the
    post-approval handoff to `documenting` in `_handle_validating`.
    """
    from orchestrator import workflow as _wf

    wt = run.worktree
    # Interrupt / timeout / still-mid-rebase dispositions park (or, for the
    # shutdown-sweep interrupt, silently drop) and signal the caller to stop.
    if _park_stalled_conflict_result(ctx, run):
        return

    after_sha = _wf._head_sha(wt)
    if not after_sha or after_sha == before_sha:
        # Agent did not finish the rebase. Treat as a question / silence park,
        # mirroring the implementing handler.
        _wf._on_question(ctx.gh, ctx.issue, ctx.state, run.dev_result)
        ctx.gh.write_pinned_state(ctx.issue, ctx.state)
        return

    dirty = _wf._worktree_dirty_files(wt)
    if dirty:
        _wf._on_dirty_worktree(ctx.gh, ctx.issue, ctx.state, run.dev_result, dirty)
        ctx.gh.write_pinned_state(ctx.issue, ctx.state)
        return

    _finalize_conflict_resolution(
        ctx, wt, after_sha, conflict_round, force_with_lease=force_with_lease,
    )


def _park_stalled_conflict_result(
    ctx: _ConflictContext, run: _ConflictResumeRun,
) -> bool:
    """Park (or silently drop) a conflict-resolution run that never landed
    a usable commit. Returns True when the tick is fully handled.

    Covers the three dispositions that precede any HEAD inspection: a
    shutdown-sweep interruption (drop the result, return WITHOUT writing
    pinned state so the rebase re-runs from durable state), an agent
    timeout, and a rebase left mid-flight. Returns False to let the caller
    inspect HEAD for a completed resolution.
    """
    from orchestrator import workflow as _wf

    dev_result = run.dev_result
    # Shutdown-sweep interruption: a conflict-resolution run the orchestrator
    # killed mid-flight has no trustworthy result, so ignore it and return
    # WITHOUT writing pinned state -- the caller's in-memory watermark /
    # session mutations are discarded and the next process re-runs the rebase
    # from durable state. Must precede the timeout / unfinished-rebase branches.
    if _wf._ignore_if_interrupted(ctx.issue, dev_result):
        return True

    if dev_result.timed_out:
        _park_conflict(
            ctx,
            f"{config.HITL_MENTIONS} dev agent timed out resolving rebase "
            f"conflicts after {config.AGENT_TIMEOUT}s; manual intervention "
            "needed.",
            reason="agent_timeout",
        )
        return True

    if not _wf._rebase_in_progress(run.worktree):
        return False

    raw = dev_result.last_message.strip()
    quoted = ""
    if raw:
        quoted = f"\n\nAgent output:\n\n{_wf._as_blockquote(raw)}"
    _park_conflict(
        ctx,
        f"{config.HITL_MENTIONS} rebase is still in progress after the "
        "dev agent returned; finish it manually or comment with "
        f"guidance to resume.{quoted}",
        reason="rebase_in_progress",
    )
    return True


def _finalize_conflict_resolution(
    ctx: _ConflictContext,
    wt: Path,
    after_sha: str,
    conflict_round: int,
    *,
    force_with_lease: Optional[str] = None,
) -> None:
    """Push a completed conflict resolution and flip to `validating`.

    Parks on push failure; on success bumps `conflict_round`, emits the
    `agent_resolved` audit event, and hands to `validating` so the
    reviewer re-runs against the resolved branch. Writes pinned state on
    every exit.
    """
    from orchestrator import workflow as _wf

    branch = _wf._resolve_branch_name(ctx.state, ctx.spec, ctx.issue.number)
    if not _wf._push_branch(ctx.spec, wt, branch, force_with_lease=force_with_lease):
        _park_conflict(
            ctx,
            f"{config.HITL_MENTIONS} git push failed after conflict "
            "resolution; see orchestrator logs.",
            reason="push_failed",
        )
        return

    # Pushed branch diff (fresh conflict resolution OR awaiting-human resume
    # that landed a commit) -> hand straight back to validating; the single
    # docs pass runs after final reviewer approval.
    _hand_resolved_round_to_validating(
        ctx, conflict_round, ctx.state.get("pr_number"),
        outcome="agent_resolved", sha=after_sha,
    )
