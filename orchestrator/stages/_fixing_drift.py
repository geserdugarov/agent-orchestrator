# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Fixing drift."""
from __future__ import annotations

from orchestrator.stages import _fixing_state as _state
from orchestrator.stages import fixing as _owner

_FixingContext = _owner._FixingContext
Optional = _owner.Optional
Path = _owner.Path
WorkflowLabel = _owner.WorkflowLabel
_AWAITING_HUMAN = _state._AWAITING_HUMAN
_CONFLICT_ROUND = _state._CONFLICT_ROUND
_PARK_REASON = _state._PARK_REASON
_REVIEW_ROUND = _state._REVIEW_ROUND


def _stale_pr_head_reason(base_ref: str, pr_head: str, local_head: str) -> str:
    """Explain a live PR head that lags an unpushed local rebase onto base."""
    pr_short = pr_head[:8]
    local_short = local_head[:8]
    return (
        f"already rebased onto `{base_ref}`, but the PR head "
        f"(`{pr_short}`) is stale (local `{local_short}`)"
    )


def _fixing_drift_reason(
    ctx: _FixingContext, wt: Path, base_ref: str,
) -> Optional[str]:
    """Classify how a clean parked `fixing` worktree has drifted from its PR,
    or return ``None`` when it is in sync (the transient park is the real
    blocker, not drift).

    Two drift shapes both reconcile via `resolving_conflict`:

      * worktree BEHIND `<remote>/<base>` -> needs a rebase.
      * worktree already rebased locally but the rewrite was never pushed, so
        local HEAD differs from the (stale) remote PR head -> needs a
        force-publish (`_handle_resolving_conflict` recognizes an already-
        rebased worktree and publishes it instead of parking).

    Trusts the once-per-tick base fetch `_refresh_base_and_worktrees` ran
    before dispatch (mirrors `_sync_worktree_with_base`, which also measures
    behind without re-fetching). A stale ref can only undercount (stay parked)
    or, on the rare case the per-tick fetch failed, overcount -- and
    `_handle_resolving_conflict` re-fetches before it acts, so an overcount
    self-corrects. The routing decision is cheap: base drift is a local
    `rev-list HEAD..<remote>/<base>`, and the unpushed-rebase check compares
    local HEAD to `pr.head.sha` (the live remote head fetched this tick).
    """
    from orchestrator import workflow as _wf

    behind_r = _wf._git("rev-list", "--count", f"HEAD..{base_ref}", cwd=wt)
    if behind_r.returncode != 0:
        return None
    try:
        behind = int((behind_r.stdout or "0").strip() or "0")
    except ValueError:
        return None

    if behind > 0:
        return f"{behind} commit(s) behind `{base_ref}`"

    # On top of base: is the local branch out of sync with the PR head? `pr`
    # was fetched fresh this tick, so `pr.head.sha` is the live remote head. A
    # mismatch means the worktree carries a rebase that was never pushed --
    # `_handle_resolving_conflict` republishes it (over a stale,
    # orchestrator-produced PR head).
    local_head = _wf._head_sha(wt) or ""
    pr_head = getattr(getattr(ctx.pr, "head", None), "sha", None) or ""
    if local_head and pr_head and local_head != pr_head:
        return _owner._stale_pr_head_reason(base_ref, pr_head, local_head)
    return None  # in sync with the PR -> genuine dev question


def _post_fixing_conflict_notice(
    ctx: _FixingContext, pr_number: int, drift_reason: str,
) -> None:
    """Post the worktree-drift reroute notice to the PR, swallowing a transient
    comment failure (the relabel still proceeds; the next tick re-fetches)."""
    from orchestrator import workflow as _wf

    try:
        _wf._post_pr_comment(
            ctx.gh, pr_number, ctx.state,
            f":mag: PR worktree is out of sync ({drift_reason}) and the `fixing` "
            "fix-loop is parked on a stuck transient condition that the "
            "self-recovery could not clear. Routing `fixing` -> "
            "`resolving_conflict` to reconcile the branch before the next "
            "reviewer round.",
        )
    except Exception:
        _wf.log.exception(
            "issue=#%s could not post worktree-drift reroute notice to PR #%s",
            ctx.issue.number, pr_number,
        )


def _route_parked_fixing_to_conflict(
    ctx: _FixingContext, drift_reason: str,
) -> None:
    """Relabel a drifted parked `fixing` worktree to `resolving_conflict` so
    its handler reconciles the branch before the next reviewer round.

    The `pending_fix_*` bookmarks and in_review watermarks are left untouched
    so the eventual `in_review` re-entry still re-discovers the feedback
    (mirrors the refresh-time conflict detour).
    """
    from orchestrator import workflow as _wf

    pr_number = int(ctx.state.get("pr_number"))
    # Seed `conflict_round` only when absent so a re-entry preserves the cap
    # counter (mirrors `_route_pr_worktree_to_resolving_conflict`).
    if ctx.state.get(_CONFLICT_ROUND) is None:
        ctx.state.set(_CONFLICT_ROUND, 0)
    ctx.state.set(_AWAITING_HUMAN, False)
    ctx.state.set(_PARK_REASON, None)
    _owner._post_fixing_conflict_notice(ctx, pr_number, drift_reason)
    ctx.gh.emit_event(
        _CONFLICT_ROUND,
        issue_number=ctx.issue.number,
        stage="fixing",
        pr_number=pr_number,
        sha=getattr(getattr(ctx.pr, "head", None), "sha", None) or None,
        action="entered",
        conflict_round=int(ctx.state.get(_CONFLICT_ROUND) or 0),
        review_round=int(ctx.state.get(_REVIEW_ROUND) or 0),
        retry_count=ctx.state.get("retry_count"),
    )
    _wf.log.info(
        "issue=#%s parked `fixing` worktree is out of sync (%s); routing -> "
        "resolving_conflict",
        ctx.issue.number, drift_reason,
    )
    ctx.gh.set_workflow_label(ctx.issue, WorkflowLabel.RESOLVING_CONFLICT)
    ctx.gh.write_pinned_state(ctx.issue, ctx.state)


def _reconcile_parked_fixing(ctx: _FixingContext) -> bool:
    """Hand a stuck validating-route transient `fixing` park to
    `resolving_conflict` on worktree drift.

    Called from the `recovery == "stuck"` branch of
    `_dispatch_validating_recovery`: `_try_recover_validating_transient_park`
    could not clear the transient condition (e.g. `push_failed` keeps
    failing), but the underlying cause may be a base advance that landed while
    the issue was parked. The per-tick base sync (`_sync_pr_worktree_to_base`)
    deliberately stands down on every `awaiting_human` park, so the integration
    work nobody else will do is stranded and the issue sits parked forever.

    Returns False (issue stays parked) when the worktree is missing, dirty (an
    operator may be inspecting a dirty-tree park), or the worktree is already
    in sync with the PR head (the transient condition is the real blocker, not
    drift). Returns True after routing the drift to `resolving_conflict`.
    """
    from orchestrator import workflow as _wf

    spec = ctx.spec
    wt = _wf._worktree_path(spec, ctx.issue.number)
    if not wt.exists():
        return False
    if _wf._worktree_dirty_files(wt):
        return False

    base_ref = f"{spec.remote_name}/{spec.base_branch}"
    drift_reason = _owner._fixing_drift_reason(ctx, wt, base_ref)
    if drift_reason is None:
        return False

    _owner._route_parked_fixing_to_conflict(ctx, drift_reason)
    return True
