# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Conflict outcomes."""
from __future__ import annotations

from orchestrator.stages import conflicts as _owner

_ConflictContext = _owner._ConflictContext
_ConflictResumeRun = _owner._ConflictResumeRun
Optional = _owner.Optional
Path = _owner.Path
config = _owner.config


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
    if _owner._park_stalled_conflict_result(ctx, run):
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

    _owner._finalize_conflict_resolution(
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
        _owner._park_conflict(
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
    _owner._park_conflict(
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
        _owner._park_conflict(
            ctx,
            f"{config.HITL_MENTIONS} git push failed after conflict "
            "resolution; see orchestrator logs.",
            reason="push_failed",
        )
        return

    # Pushed branch diff (fresh conflict resolution OR awaiting-human resume
    # that landed a commit) -> hand straight back to validating; the single
    # docs pass runs after final reviewer approval.
    _owner._hand_resolved_round_to_validating(
        ctx, conflict_round, ctx.state.get("pr_number"),
        outcome="agent_resolved", sha=after_sha,
    )
