# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Anchoring one auto-rebase, and the two ways starting it can end badly.

The pre-rebase SHA is the whole reason these three helpers live together. It
is the lease a later force-push is pinned to and the anchor a crashed tick is
recovered from, so it has to be readable before git is allowed to move HEAD
and pinned before the rewrite runs -- an attempt that mutated the worktree
first and recorded the anchor second would leave a tick that died in between
with a rewritten branch nobody can compare against. Reading it fails closed,
and a rebase that then fails is aborted back onto it before the outcome is
routed: conflicted files are the dev agent's work, anything else is a park.
"""
from __future__ import annotations

from github.PullRequest import PullRequest

from orchestrator import config
from orchestrator.git import commands
from orchestrator.git.base_sync import conflicts, persistence, pre_pr
from orchestrator.git.base_sync.models import _AutoRebaseContext
from orchestrator.git.base_sync.state import (
    _AWAITING_HUMAN,
    _PARK_REASON,
    _PENDING_PUSH_SHA,
    _PENDING_REWRITE_PR,
    _PENDING_REWRITE_SHA,
    _PENDING_REWRITE_STAGE,
    _REASON_AUTO_BASE_REBASE_FAILED,
    log,
)
from orchestrator.git.verification import probes


def _park_unreadable_pre_rebase_head(context: _AutoRebaseContext) -> None:
    """Fail closed when the lease and recovery anchor cannot be read."""
    log.error(
        "issue=#%d cannot read local HEAD before auto base rebase; "
        "parking awaiting human (no rebase attempted)",
        context.issue.number,
    )
    spec = context.spec
    persistence._park_auto_rebase_failure(
        context.gh,
        context.issue,
        context.state,
        message=(
            f"{config.HITL_MENTIONS} PR #{context.pr_number} is "
            f"{context.behind} commit(s) behind "
            f"`{spec.remote_name}/{spec.base_branch}`, "
            "but the orchestrator could not read local `HEAD` on "
            "the per-issue worktree before attempting the auto "
            "rebase. Force-with-lease pushes and the crash-recovery "
            "anchor both require a known pre-rebase SHA, so the "
            "rebase was skipped. Inspect the worktree's git state "
            "and reply on this issue with anything to retry."
        ),
        reason=_REASON_AUTO_BASE_REBASE_FAILED,
    )


def _record_auto_rebase_attempt(
    context: _AutoRebaseContext,
    before_sha: str,
    consumed_comment_id: int | None,
) -> None:
    """Persist the anchor, the attempt's terms, and any retry unpark.

    All of it before git runs, because every field here is something the
    branch moving would make unanswerable. The anchor is the head the pull
    request is standing on and the head the force-push behind this rebase is
    leased against. The TERMS beside it -- the pull request this attempt
    publishes onto and the stage it was entered from -- are what the permit's
    publication checks are asked against on the tick after a crash: read off
    the issue then they would compare today with today, and a relabel or a
    repoint made while the process was down would pass as this tick's own.

    They go down here rather than with the head the rebase produces, and that
    is what makes the window between `git rebase` returning and the write
    recording its output recoverable at all. A crash there leaves a checkout
    on a replay nothing names -- but the terms on the comment still say which
    publication the attempt in flight was for, so the recovery can assemble
    the transfer evidence over the dead tick's own publication and let the
    permit prove the head by what it contributes.
    """
    if consumed_comment_id is not None:
        context.state.set("last_action_comment_id", consumed_comment_id)
        context.state.set(_AWAITING_HUMAN, False)
        context.state.set(_PARK_REASON, None)
    context.state.set(_PENDING_PUSH_SHA, before_sha)
    context.state.set(_PENDING_REWRITE_PR, context.pr_number)
    context.state.set(_PENDING_REWRITE_STAGE, str(context.label))
    context.gh.write_pinned_state(context.issue, context.state)


def _handle_failed_auto_rebase(
    context: _AutoRebaseContext,
    pr: PullRequest,
    conflicted_files: list[str],
) -> None:
    """Abort a failed rebase, then route conflicts or park other failures."""
    abort = commands._git_hardened("rebase", "--abort", cwd=context.worktree)
    if abort.returncode != 0:
        log.warning(
            "issue=#%d base rebase failed and abort failed: %s",
            context.issue.number,
            (abort.stderr or "").strip(),
        )
    persistence._clears_the_attempt(context.state)
    if conflicted_files:
        conflicts._route_pr_worktree_to_resolving_conflict(
            context.gh,
            context.spec,
            context.issue,
            context.state,
            context.pr_number,
            label=context.label,
            behind=context.behind,
            conflicted_files=conflicted_files,
            pr_head_sha=getattr(pr.head, "sha", None) or None,
        )
        return

    log.warning(
        "issue=#%d base rebase failed without conflicted files; "
        "parking awaiting human (refresh-only recovery on a new "
        "issue comment)",
        context.issue.number,
    )
    spec = context.spec
    persistence._park_auto_rebase_failure(
        context.gh,
        context.issue,
        context.state,
        message=(
            f"{config.HITL_MENTIONS} PR #{context.pr_number} is "
            f"{context.behind} commit(s) behind "
            f"`{spec.remote_name}/{spec.base_branch}` "
            "and the auto rebase failed for a non-conflict reason "
            "(planted hook, smudge filter, permissions, ...). The "
            "worktree was restored to the pre-rebase SHA via "
            "`git rebase --abort`. Investigate the worktree / hooks, "
            "then reply on this issue with anything once the "
            "underlying problem is fixed; the next polling tick will "
            "re-attempt the auto rebase."
        ),
        reason=_REASON_AUTO_BASE_REBASE_FAILED,
    )


def _record_the_rewrite(context: _AutoRebaseContext) -> str:
    """Say which commit this attempt produced, durably, as git hands it back.

    The anchor pinned before git ran is what brings an interrupted attempt
    back; it is not what says the checkout it comes back to is this attempt's
    own work. A rebase REPLAYS the branch, so the commit the pull request
    still carries is on no local history afterwards and the two look diverged
    -- which is exactly what a checkout somebody else left, a worktree rebuilt
    from elsewhere, and an operator's reset look like too. Told those apart by
    the divergence alone, a recovery would force-push whatever it found over
    the candidate on the remote, under a lease the anchor happily satisfies.

    So the head goes down as a record of its own, and it goes down HERE:
    on the statement after `git rebase` returns, before the head is read for
    anything else and before any guard that could refuse. Every window a crash
    can be lost in from this point on is behind it, and the one it cannot
    cover -- between git returning and this write -- is answered by the terms
    the anchor already carries: the attempt reads as one still IN FLIGHT, and
    the head it left has to be vouched for by what it contributes rather than
    by an id nobody wrote down. Where nothing can vouch for it, the recovery
    falls back to the counts alone: a strictly-ahead branch is a fast-forward
    the anchor lease loses nothing to, and a divergent one resets and
    parks.

    Nothing is recorded for a rebase that moved nothing or left a head this
    host cannot read. The first is the no-op the guard behind this finishes by
    dropping the attempt, and the second names no commit to record. Both leave
    the terms standing, which is right: the attempt is not over until the step
    that ends it drops the whole group.
    """
    after_sha = probes._head_sha(context.worktree) or ""
    if not after_sha or after_sha == context.state.get(_PENDING_PUSH_SHA):
        return after_sha
    context.state.set(_PENDING_REWRITE_SHA, after_sha)
    context.gh.write_pinned_state(context.issue, context.state)
    return after_sha


def _start_auto_rebase(
    context: _AutoRebaseContext,
    pr: PullRequest,
    consumed_comment_id: int | None,
) -> str | None:
    """Anchor and execute the rebase, returning the known pre-rebase SHA.

    The replay it produced is recorded on the statement after git hands it
    back, which is what makes a crash there recoverable: a rebase REPLAYS the
    branch, so what it leaves diverges from the head the pull request still
    carries, and without the record the tick that comes back cannot tell that
    divergence from a worktree somebody rebuilt.
    """
    before_sha = probes._head_sha(context.worktree) or ""
    if not before_sha:
        _park_unreadable_pre_rebase_head(context)
        return None
    _record_auto_rebase_attempt(context, before_sha, consumed_comment_id)
    succeeded, conflicted_files = pre_pr._rebase_base_into_worktree(
        context.spec, context.worktree,
    )
    if not succeeded:
        _handle_failed_auto_rebase(context, pr, conflicted_files)
        return None
    _record_the_rewrite(context)
    return before_sha
