# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Documenting drift."""
from __future__ import annotations

from orchestrator.stages import _documenting_state as _state
from orchestrator.stages import documenting as _owner

_DocumentingContext = _owner._DocumentingContext
WorkflowLabel = _owner.WorkflowLabel
config = _owner.config
filter_trusted = _owner.filter_trusted
suppress = _owner.suppress
_AWAITING_HUMAN = _state._AWAITING_HUMAN
_LAST_ACTION_COMMENT_ID = _state._LAST_ACTION_COMMENT_ID
_PARK_REASON = _state._PARK_REASON


def _documenting_drift_fetch(ctx: _DocumentingContext, wt) -> bool:
    """Fetch `<remote>/<branch>` before the drift-unwind ahead/behind probe.

    Returns True on success; on a fetch failure parks with `fetch_failed` and
    returns False -- a stale local docs commit against the OLD body silently
    riding into the next approval is worse than parking.
    """
    from orchestrator import workflow as _wf

    spec = ctx.spec
    branch = ctx.branch
    fetch_branch = _wf._authed_fetch(
        spec,
        f"+refs/heads/{branch}:refs/remotes/{spec.remote_name}/{branch}",
        cwd=wt,
    )
    if fetch_branch.returncode != 0:
        _wf.log.error(
            "issue=#%d documenting drift fetch failed: %s",
            ctx.issue.number, (fetch_branch.stderr or "").strip(),
        )
        _owner._park_documenting(
            ctx,
            f"{config.HITL_MENTIONS} `git fetch "
            f"{spec.remote_name} {branch}` failed while routing "
            "documenting drift back to `validating`; the local "
            "worktree may carry an unpushed docs commit against "
            "the OLD body -- see orchestrator logs.",
            "fetch_failed",
        )
        return False
    return True


def _documenting_drift_probe(ctx: _DocumentingContext, wt):
    """Probe the worktree's ahead/behind vs. `<remote>/<branch>`.

    Run the ahead/behind probe inline (rather than via `_branch_ahead_behind`)
    so a probe failure is distinguishable from a real "in sync" result:
    `_branch_ahead_behind` swallows git errors as `(0, 0)`, which would
    silently let an unpushed local docs commit against the OLD body survive
    into the next final-docs hop's recovered-commit shortcut. Use the same git
    invocation but check the exit code + parse here.

    Returns `(ahead, behind)` on success; on a probe failure parks with
    `worktree_reset_failed` and returns None.
    """
    from orchestrator import workflow as _wf

    spec = ctx.spec
    branch = ctx.branch
    probe = _wf._git_hardened(
        "rev-list", "--left-right", "--count",
        f"refs/remotes/{spec.remote_name}/{branch}...HEAD",
        cwd=wt,
    )
    parts = (probe.stdout or "").strip().split()
    if probe.returncode == 0 and len(parts) == 2:
        with suppress(ValueError):
            return int(parts[1]), int(parts[0])
    _wf.log.error(
        "issue=#%d documenting drift ahead/behind probe "
        "failed (rc=%s stderr=%s stdout=%s)",
        ctx.issue.number, probe.returncode,
        (probe.stderr or "").strip(),
        (probe.stdout or "").strip(),
    )
    _owner._park_documenting(
        ctx,
        f"{config.HITL_MENTIONS} could not probe local vs. "
        f"`{spec.remote_name}/{branch}` while routing "
        "documenting drift back to `validating`; the local "
        "worktree may carry an unpushed docs commit against "
        "the OLD body -- see orchestrator logs.",
        "worktree_reset_failed",
    )
    return None


def _documenting_drift_hard_reset(ctx: _DocumentingContext, wt) -> bool:
    """Hard-reset + clean the worktree to `<remote>/<branch>`.

    `git reset --hard` drops local docs commits / tracked edits; the follow-up
    `git clean -fd` removes untracked docs files and any under-`docs/` subdirs
    the docs agent created but the reviewer never approved. Returns True on
    success; on a git failure parks with `worktree_reset_failed` and returns
    False.
    """
    from orchestrator import workflow as _wf

    spec = ctx.spec
    branch = ctx.branch
    reset = _wf._git_hardened(
        "reset", "--hard", f"{spec.remote_name}/{branch}", cwd=wt,
    )
    if reset.returncode != 0:
        _wf.log.error(
            "issue=#%d documenting drift reset failed "
            "(rc=%s stderr=%s)",
            ctx.issue.number, reset.returncode,
            (reset.stderr or "").strip(),
        )
        _owner._park_documenting(
            ctx,
            f"{config.HITL_MENTIONS} `git reset --hard "
            f"{spec.remote_name}/{branch}` failed while "
            "routing documenting drift back to "
            "`validating`; the local worktree still "
            "carries docs work against the OLD body -- "
            "see orchestrator logs.",
            "worktree_reset_failed",
        )
        return False
    clean = _wf._git_hardened("clean", "-fd", cwd=wt)
    if clean.returncode != 0:
        _wf.log.error(
            "issue=#%d documenting drift clean failed "
            "(rc=%s stderr=%s)",
            ctx.issue.number, clean.returncode,
            (clean.stderr or "").strip(),
        )
        _owner._park_documenting(
            ctx,
            f"{config.HITL_MENTIONS} `git clean -fd` "
            "failed while routing documenting drift back "
            "to `validating`; the local worktree may "
            "still carry untracked docs files against "
            "the OLD body -- see orchestrator logs.",
            "worktree_reset_failed",
        )
        return False
    return True


def _reset_documenting_drift_worktree(
    ctx: _DocumentingContext, wt,
) -> bool:
    """Reconcile the PR worktree to `<remote>/<branch>` while routing
    documenting drift back to `validating`.

    A recovered local docs commit (a prior tick committed but parked
    before the push landed -- ahead > 0 vs. `<remote>/<branch>`) was
    authored against the OLD body; leaving it on disk would let the next
    final-docs tick's recovered-commit shortcut push it without ever
    spawning a fresh docs agent against the new requirements --
    especially under `SQUASH_ON_APPROVAL=off`, where the
    reviewer-approved head is the dev's PR head (no rewrite gap), so the
    recovered docs commit applies cleanly on top of the next approval.
    Fetch the branch, probe ahead/behind, and hard-reset + clean any
    local docs work (including uncommitted / untracked edits) so the next
    approved round starts from the actual PR head.

    Reset whenever the worktree is ahead (a recovered commit), behind (the
    remote PR head moved past local HEAD, so the reviewer must re-evaluate the
    actual head), or dirty (`_worktree_dirty_files` surfaces both
    modified-tracked and untracked paths, so any non-empty list is a cleanup
    trigger).

    Returns True on success (worktree in sync). Returns False when a git
    step failed and the issue was parked -- a stale local commit silently
    riding into the next approval is worse than parking.
    """
    from orchestrator import workflow as _wf

    if not _owner._documenting_drift_fetch(ctx, wt):
        return False
    probe = _owner._documenting_drift_probe(ctx, wt)
    if probe is None:
        return False
    ahead, behind = probe
    dirty = _wf._worktree_dirty_files(wt)
    if ahead > 0 or behind > 0 or dirty:
        return _owner._documenting_drift_hard_reset(ctx, wt)
    return True


def _announce_documenting_drift(
    ctx: _DocumentingContext, new_hash: str,
) -> None:
    """Record the new body hash, post the re-route notice, and mark the
    issue-thread comments consumed for a freshly-detected drift."""
    from orchestrator import workflow as _wf

    ctx.state.set("user_content_hash", new_hash)
    _wf._post_issue_comment(
        ctx.gh, ctx.issue, ctx.state,
        ":pencil2: issue body changed; routing back to "
        "`validating` so the reviewer re-evaluates the "
        "updated requirements.",
    )
    _wf._mark_drift_comments_consumed(ctx.gh, ctx.issue, ctx.state)


def _begin_documenting_drift_unwind(ctx: _DocumentingContext) -> None:
    """Seed the drift-unwind sentinel and drop the stale approval.

    Set `docs_drift_unwind_pending` so an operator unpark or a later human
    comment (without a fresh drift) re-enters the drift block on the next tick
    and retries the reconcile + relabel; the marker is cleared ONLY on the
    success path that relabels to `validating`. Without it, an operator unpark
    on a failed reconcile would fall through to the normal flow and advance to
    `in_review` against the OLD body, skipping the required `validating`
    re-review.

    Clear `review_round` BEFORE any fallible cleanup (fetch / reset): drift
    means the prior reviewer approval is stale regardless of whether the
    on-disk reset succeeds, so the round counter must drop now -- an operator
    unpark or manual relabel after a fetch failure must not be able to ride
    the stale approval into a final-docs handoff that skips the re-review.
    """
    state = ctx.state
    state.set("docs_drift_unwind_pending", True)
    state.set(_AWAITING_HUMAN, False)
    state.set(_PARK_REASON, None)
    state.set("review_round", 0)


def _reconcile_documenting_drift(ctx: _DocumentingContext) -> bool:
    """Docs drift detection + unwind back to `validating`.

    User-content drift: a human edited the issue title/body while the
    final-docs hop was in flight. The reviewer approved the OLD
    requirements, so the docs pass would be running against a body the
    reviewer never saw. Mirror `_handle_in_review`'s drift invalidation:
    reset `review_round=0`, post the notice, mark issue-thread comments
    consumed, refresh the baseline hash, reconcile the worktree, and
    relabel to `validating` so the reviewer re-evaluates the updated body
    on the next tick. Do NOT spawn the docs agent: the prior approval is
    gone and a docs commit on top would just need to be re-reviewed
    alongside any impl change.

    Returns True when the drift path fully handled this tick (the silent
    fast-path, a reconcile park, or the relabel to `validating`); False
    when there is no drift and the normal docs flow should continue.
    """
    from orchestrator import workflow as _wf

    new_hash = _wf._detect_user_content_change(ctx.gh, ctx.issue, ctx.state)
    fresh_drift = new_hash is not None
    pending_unwind = bool(ctx.state.get("docs_drift_unwind_pending"))
    # A prior tick's drift unwind couldn't finish (the worktree reconcile
    # failed and parked) and nothing fresh has happened: stay silent so the
    # parked state survives operator inspection without re-posting the same
    # park comment every tick. Only a trusted reply is the "retry the unwind"
    # signal -- with `ALLOWED_ISSUE_AUTHORS` set an outsider comment must not
    # fall through to the reconcile-retry below.
    if pending_unwind and not fresh_drift and ctx.state.get(_AWAITING_HUMAN):
        last_action_id = ctx.state.get(_LAST_ACTION_COMMENT_ID)
        if not filter_trusted(ctx.gh.comments_after(ctx.issue, last_action_id)):
            return True
    if not (fresh_drift or pending_unwind):
        return False

    if fresh_drift:
        _owner._announce_documenting_drift(ctx, new_hash)
    _owner._begin_documenting_drift_unwind(ctx)
    wt = _wf._worktree_path(ctx.spec, ctx.issue.number)
    if wt.exists() and not _owner._reset_documenting_drift_worktree(ctx, wt):
        return True
    # Reconcile succeeded (or the worktree didn't exist): the drift unwind is
    # complete, clear the sentinel and relabel.
    ctx.state.set("docs_drift_unwind_pending", False)
    ctx.gh.set_workflow_label(ctx.issue, WorkflowLabel.VALIDATING)
    ctx.gh.write_pinned_state(ctx.issue, ctx.state)
    return True
