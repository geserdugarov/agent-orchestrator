# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Conflict divergence."""
from __future__ import annotations

from orchestrator.stages import conflicts as _owner

_ConflictContext = _owner._ConflictContext
_DivergeDecision = _owner._DivergeDecision
_WorktreeSync = _owner._WorktreeSync
Optional = _owner.Optional
Path = _owner.Path
config = _owner.config


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
        and _owner._pr_head_orchestrator_produced(ctx.state, pr)
        and _owner._already_rebased_onto_base(ctx.spec, sync.worktree)
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

    _owner._park_diverged_worktree(ctx, pr, sync)
    return _DivergeDecision(parked=True)


def _park_diverged_worktree(
    ctx: _ConflictContext, pr, sync: _WorktreeSync,
) -> None:
    """Park a stale / diverged worktree: force-pushing the local state would
    clobber the real PR head."""
    spec = ctx.spec
    pr_head_short = pr.head.sha[:8]
    _owner._park_conflict(
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
        _owner._park_conflict(
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
        _owner._park_conflict(
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
    still_behind = _owner._still_behind_base(wt, base_ref)
    if still_behind != 0:
        _wf.log.info(
            "issue=#%d resolving_conflict: pushed %d recovered commit(s) "
            "but worktree still %d behind %s; continuing with base rebase",
            ctx.issue.number, sync.ahead, still_behind, base_ref,
        )
        return False
    # Pushed branch diff -> hand straight back to validating; the single docs
    # pass runs after final reviewer approval.
    _owner._hand_resolved_round_to_validating(
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
