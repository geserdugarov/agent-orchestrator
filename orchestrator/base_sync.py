# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Per-tick base refresh and PR-aware rebase routing.

Owns the helpers that drive the pre-tick base sync:

* `_rebase_base_into_worktree` -- run `git rebase origin/<base>` in a
  worktree and report whether it succeeded plus the conflicted paths.
* `_merge_base_into_worktree` -- compatibility alias for older
  patches / imports that targeted the pre-rebase name.
* `_rebase_in_progress` -- detect a worktree left mid-rebase by a
  prior tick or by an agent.
* `_refresh_base_and_worktrees` -- fetch `origin/<base>` once per tick
  per spec and dispatch each per-issue worktree.
* `_PR_REFRESH_DETOUR_LABELS` -- the workflow labels whose PR worktrees
  the per-tick refresh is willing to drive through the rebase flow.
* `_sync_worktree_with_base` -- per-worktree dispatch: pre-PR rebase or
  PR-having clean-rebase / conflict detour, with skip rules for dirty
  trees, `backlog`, `hold_base_sync`, and the `question` label.
* `_sync_pr_worktree_to_base` -- for a behind-base PR-having issue,
  attempt a local rebase + push (force-with-lease); on a clean rebase
  reset `review_round` and relabel to `validating`; only relabel to
  `resolving_conflict` when the rebase actually leaves conflicted files.

Imports the hardened git subprocess layer from `git_plumbing.py`, the
worktree-layout helpers from `worktree_lifecycle.py`, the worktree-
state probes (`_worktree_dirty_files`, `_head_sha`) from `verify.py`,
the branch-publication helpers (`_push_branch`) from `git_plumbing.py`,
and the PR-comment helper from `workflow_messages.py`. `worktrees.py`
re-exports every name below under its original name so existing imports
(`from orchestrator.worktrees import _refresh_base_and_worktrees`) and
`patch.object(worktrees, "_foo", ...)` test patches that still target
the worktrees module keep resolving the symbol -- but the actual call
graph lives here, so test patches that need to INTERCEPT a call from
inside `_refresh_base_and_worktrees` / `_sync_worktree_with_base` /
`_sync_pr_worktree_to_base` should target this module (`base_sync`)
directly.

Helpers remain prefixed with `_` because they are module-internal
contracts -- the public surface (the dispatcher entry points and the
stage handlers they route to) still lives in `workflow.py` and
`orchestrator/stages/`.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional, Tuple

from github.Issue import Issue

from . import config
from .config import RepoSpec
from .branch_publication import _branch_ahead_behind
from .git_plumbing import (
    _authed_fetch,
    _authed_target_fetch,
    _git,
    _git_hardened,
    _push_branch,
)
from .github import (
    BACKLOG_LABEL,
    BASE_SYNC_HOLD_LABEL,
    GitHubClient,
    PinnedState,
    issue_has_label,
)
from .scheduler import IssueScheduler
from .verify import _head_sha, _worktree_dirty_files
from .workflow_messages import _post_pr_comment
from .worktree_lifecycle import _branch_name, _repo_worktrees_root

log = logging.getLogger(__name__)


def _rebase_base_into_worktree(
    spec: RepoSpec, worktree: Path
) -> Tuple[bool, list[str]]:
    """Run `git rebase origin/<base>` in the worktree.

    Returns `(succeeded, conflicted_files)`. On success, `conflicted_files`
    is empty -- whether the rebase was a no-op or replayed commits is the
    caller's job to detect via the HEAD-SHA delta. On failure, the
    conflicted-file list is the unmerged paths from
    `git diff --name-only --diff-filter=U`; an empty list means the rebase
    failed for a non-conflict reason (hooks, permissions, etc.) and the
    caller should park rather than ask the agent to resolve nothing.

    Both subprocess calls run under `_git_hardened`: the diff is
    read-only but still executes inside an agent-writable worktree, so
    a planted hooksPath / fsmonitor would otherwise execute attacker
    code under the orchestrator's UID at diff time.
    """
    r = _git_hardened(
        "rebase",
        f"{spec.remote_name}/{spec.base_branch}", cwd=worktree,
    )
    if r.returncode == 0:
        return True, []
    conflicted = _git_hardened(
        "diff", "--name-only", "--diff-filter=U", cwd=worktree,
    )
    files = [
        line.strip() for line in (conflicted.stdout or "").splitlines()
        if line.strip()
    ]
    return False, files


def _merge_base_into_worktree(
    spec: RepoSpec, worktree: Path
) -> Tuple[bool, list[str]]:
    """Compatibility alias for older patches/imports.

    TODO(remove after 2026-08-24): drop once out-of-repo patches have moved
    to `_rebase_base_into_worktree`.
    """
    return _rebase_base_into_worktree(spec, worktree)


def _rebase_in_progress(worktree: Path) -> bool:
    """Return True when the worktree still has an unfinished rebase."""
    for state_dir in ("rebase-merge", "rebase-apply"):
        r = _git_hardened("rev-parse", "--git-path", state_dir, cwd=worktree)
        if r.returncode != 0:
            continue
        path = (r.stdout or "").strip()
        if not path:
            continue
        state_path = Path(path)
        if not state_path.is_absolute():
            state_path = worktree / state_path
        if state_path.exists():
            return True
    return False


def _refresh_base_and_worktrees(
    gh: GitHubClient,
    spec: RepoSpec,
    *,
    scheduler: Optional[IssueScheduler] = None,
) -> None:
    """Fetch `origin/<base>` once for the spec and bring every existing
    per-issue worktree up to date.

    Runs at the start of each tick so a base-branch update on the remote
    propagates into in-flight issue worktrees. The per-stage
    `_ensure_*_worktree` helpers only fetch base on (re)creation, so a
    worktree that survives across ticks would otherwise stay anchored at
    whatever `origin/<base>` looked like when it was first added.

    Two paths depending on whether a PR already exists for the issue:

    * **Pre-PR worktrees** (no `pr_number` in pinned state): rebase
      the local worktree onto `origin/<base>` -- no remote yet, so there
      is nothing to push.

    * **PR-having worktrees** (validating / documenting / in_review /
      fixing): rebasing
      locally WITHOUT pushing would diverge local HEAD from `pr.head.sha` and
      break the validating reviewer (it reads local HEAD, so it would
      review a SHA that isn't on the PR) and
      `_squash_and_force_push`'s `--force-with-lease=<original_head>`
      (the lease compares against the un-rebased remote tip). So
      `_sync_pr_worktree_to_base` attempts the rebase in the refresh
      itself: on a clean rebase it pushes (force-with-lease pinned to
      the pre-rebase SHA), resets `review_round`, and relabels to
      `validating` so the reviewer re-runs against the rewritten
      branch directly; the single docs pass is deferred to the post-
      approval handoff to `documenting` in `_handle_validating`. Only
      when the rebase actually leaves conflicted files does the issue
      get relabeled to `resolving_conflict` -- the
      `_handle_resolving_conflict` handler then drives the dev agent to
      resolve the conflict. Applying the `hold_base_sync` label to an
      issue pauses both the pre-PR local rebase and the PR refresh
      flow until the label is removed. Issues already labeled
      `resolving_conflict` are left alone (the handler runs this tick
      anyway); other labels are skipped (no PR worktree to refresh in
      those states).

    Rebase keeps the PR history linear after sibling PRs land. Every
    pushed rebase resets `review_round`, so the reviewer must re-run
    against the rewritten SHA before any merge gate can pass.

    Conflicts on the pre-PR path abort the rebase so the worktree stays
    on its original SHA -- conflict resolution still belongs to
    `_handle_resolving_conflict`. Dirty worktrees are skipped so a
    crash-recovered tree with uncommitted edits is never disturbed
    (mirrors `_on_dirty_worktree`'s rule). All failures are logged at
    info/warning and swallowed: keeping every issue moving matters more
    than perfect base sync.

    `scheduler`, when supplied, is consulted before each per-issue
    worktree sync: an issue whose handler is currently in flight in
    that scheduler is skipped this tick. Without this gate, a polling
    pass can rebase a pre-PR worktree under a still-running agent or
    relabel/state-mutate a PR worktree while its handler is still
    running, racing the base refresh against the live worker. The
    scheduler's `submit` path also rejects a duplicate active issue,
    so the workflow handler itself does not run for the in-flight
    issue this tick -- the refresh skip keeps the worktree contract
    matching that "active issues are skipped until completion"
    guarantee. `None` preserves the legacy behavior so direct test
    invocations that supply no scheduler still refresh every worktree.
    """
    fetch_r = _authed_target_fetch(spec, spec.base_branch)
    if fetch_r.returncode != 0:
        log.warning(
            "repo=%s base fetch of %s/%s failed: %s",
            spec.slug, spec.remote_name, spec.base_branch,
            (fetch_r.stderr or "").strip(),
        )
        return

    root = _repo_worktrees_root(spec)
    if not root.exists():
        return

    for wt in sorted(root.iterdir()):
        if not wt.is_dir() or not wt.name.startswith("issue-"):
            continue
        try:
            issue_number = int(wt.name[len("issue-"):])
        except ValueError:
            continue
        if scheduler is not None and scheduler.is_active(
            spec.slug, issue_number,
        ):
            # The handler for this issue is still running on a
            # scheduler worker thread. Rebasing the pre-PR worktree
            # would race the agent's working copy; the PR-having
            # detour would relabel / write pinned state while the
            # handler is mid-write. Skip the sync this tick -- the
            # next polling pass picks it up once the worker exits.
            log.debug(
                "repo=%s issue=#%d active in scheduler; skipping base "
                "sync until the worker completes", spec.slug, issue_number,
            )
            continue
        try:
            _sync_worktree_with_base(gh, spec, wt, issue_number)
        except Exception:
            log.exception(
                "repo=%s issue=#%d base sync failed; continuing",
                spec.slug, issue_number,
            )


# Workflow labels whose PR worktrees the pre-tick refresh is willing to
# rebase + push directly (and, only when the rebase leaves conflicted
# files, relabel to `resolving_conflict`). Validating, documenting,
# in_review, and fixing are the PR-stage labels: validating may run
# the reviewer again, documenting is the brief final-docs hop between
# reviewer approval and `in_review`, in_review is parked waiting for
# the HITL ready-ping and the human's manual merge, and fixing is
# between in_review and validating while a PR feedback round is being
# addressed. Documenting only checks ahead/behind vs. the PR branch
# (not the base) itself, so without this refresh-time rebase a
# sibling-PR merge during the docs pass would leave the docs commit
# on a stale base and only the next in_review tick would catch it;
# including the label here means only the `hold_base_sync` control
# label gates a PR-stage worktree's auto-rebase. `resolving_conflict`
# itself is excluded -- the handler runs this tick regardless and will
# do the rebase anyway. Other labels mean either no PR yet (pre-PR
# path applies instead) or terminal (done/rejected, nothing to refresh).
_PR_REFRESH_DETOUR_LABELS = frozenset(
    {"validating", "documenting", "in_review", "fixing"},
)


# Park reasons owned by `_sync_pr_worktree_to_base`. When the refresh
# parks an issue with one of these, no stage handler knows how to
# reconcile the underlying condition -- the recovery path is "human
# fixes the divergence, then comments on the issue; the next refresh
# tick clears the park and re-attempts the rebase". The refresh itself
# is the only place that drives that recovery, so we keep the set
# local. Other park reasons (`unmergeable`, `agent_question`,
# `review_cap`, `push_failed` / `agent_timeout` / `reviewer_timeout` /
# `reviewer_failed` for the validating recovery branch, etc.) are NOT
# in this set: they are handled by the respective stage handlers, and
# the refresh deliberately leaves those parks alone.
_AUTO_REBASE_PARK_REASONS = frozenset(
    {
        "auto_base_rebase_failed",
        "auto_base_rebase_dirty",
        "auto_base_rebase_push_failed",
    },
)


def _park_auto_rebase_failure(
    gh: GitHubClient,
    issue: Issue,
    state: PinnedState,
    *,
    message: str,
    reason: str,
) -> None:
    """Park an issue awaiting human for an auto-rebase failure.

    Wraps `_park_awaiting_human` so every refresh-time failure mode
    parks identically: `awaiting_human=True`, the HITL message lands
    on the issue thread (NOT the PR -- the resume-on-human-reply
    scan reads from the issue), `last_action_comment_id` is ratcheted
    forward by `_park_awaiting_human`, and the durable
    `park_reason` is re-set after the helper clears it by contract.
    `gh.write_pinned_state` is called here so the caller can return
    immediately.

    `reason` must be one of `_AUTO_REBASE_PARK_REASONS` -- the refresh
    recovery branch keys off the same set to decide whether a new
    human comment on this issue is the "retry now" signal.
    """
    # Lazy import: `workflow` imports `base_sync` at module load time,
    # so a top-level `from . import workflow` would be a circular
    # import. Stage modules use the same late-bind pattern.
    from . import workflow as _wf
    assert reason in _AUTO_REBASE_PARK_REASONS, (
        f"_park_auto_rebase_failure called with reason={reason!r}, "
        f"which is not in _AUTO_REBASE_PARK_REASONS"
    )
    _wf._park_awaiting_human(gh, issue, state, message, reason=reason)
    state.set("park_reason", reason)
    gh.write_pinned_state(issue, state)


def _recover_pending_auto_base_rebase(
    gh: GitHubClient,
    spec: RepoSpec,
    issue: Issue,
    state: PinnedState,
    worktree: Path,
    *,
    pr_number: int,
    label: str,
    pending_pre_rebase_sha: str,
    behind: int = 0,
    unparking_consumed_max: Optional[int] = None,
) -> bool:
    """Finalize a clean auto-base-rebase interrupted by a prior crash.

    Called by `_sync_pr_worktree_to_base` when the pinned state carries
    a `pending_auto_base_rebase_push_sha` from a previous tick. The
    flag is set BEFORE `_rebase_base_into_worktree` and cleared on
    every exit; a crash between then and the post-push state write
    leaves the worktree in one of four shapes, each handled here:

      1. Local HEAD == `pending_pre_rebase_sha` -- rebase was reverted
         (or never moved HEAD). Nothing to recover. Clear the flag and
         let the caller's normal flow re-attempt the rebase on this
         same tick.
      2. Local HEAD == remote PR head AND != `pending_pre_rebase_sha`
         -- push went through on a prior tick; the relabel /
         `review_round=0` write didn't. Finalize: clear flag, reset
         `review_round`, post a recovery PR notice, emit
         `base_rebased`, flip the label to `validating`.
      3. Local HEAD ahead of remote PR head -- rebase done, push
         didn't (or push succeeded but the remote ref was rolled back
         out-of-band before the next tick). Push with
         `--force-with-lease=<pending_pre_rebase_sha>` and finalize on
         success.
      4. Local HEAD behind or diverged from remote PR head -- someone
         else updated the PR branch out-of-band. Reset HEAD back to
         the pre-rebase SHA and park awaiting human.

    Returns True when recovery finalized / parked (caller must return
    immediately); False only for case 1 above (caller should continue
    with the normal flow on this same tick).
    """
    if label not in _PR_REFRESH_DETOUR_LABELS:
        # Operator manually relabeled (e.g. to `resolving_conflict`
        # for an unrelated reason). Clear the flag without acting.
        state.set("pending_auto_base_rebase_push_sha", None)
        gh.write_pinned_state(issue, state)
        log.info(
            "issue=#%d auto-rebase recovery: label %r is no longer in "
            "the refresh-driven set; clearing pending flag",
            issue.number, label,
        )
        return True

    branch = _branch_name(issue.number)

    def _abort_recovery_unverified(detail: str) -> bool:
        """Reset HEAD to the pre-rebase anchor and park awaiting human.

        The four 'cannot positively verify the recovery target' paths
        below (fetch failed, rev-parse failed, empty remote SHA,
        `(0, 0)` ahead/behind with a SHA mismatch) must NOT just
        `return True` without acting: that leaves the issue unparked
        and the same-tick stage handler dispatch can run on a local
        HEAD that we have NOT confirmed is published on the PR.
        Reset HEAD back to `pending_pre_rebase_sha` (= the local
        HEAD before the rebase = the remote PR head at the time the
        anchor was set) and park awaiting human; the operator's
        reply on the issue thread later un-parks via the refresh's
        `_AUTO_REBASE_PARK_REASONS` recovery branch and re-attempts
        the rebase fresh. The anchor itself is cleared because the
        reset puts HEAD at it, so a follow-up tick that finds the
        flag would just hit case 1 anyway.
        """
        reset = _git_hardened(
            "reset", "--hard", pending_pre_rebase_sha, cwd=worktree,
        )
        if reset.returncode != 0:
            log.error(
                "issue=#%d auto-rebase recovery abort: reset to pre-"
                "rebase SHA `%s` failed: %s; the park below still "
                "short-circuits same-tick handler dispatch via "
                "`awaiting_human` but operator inspection of HEAD is "
                "needed",
                issue.number, pending_pre_rebase_sha[:8],
                (reset.stderr or "").strip(),
            )
        state.set("pending_auto_base_rebase_push_sha", None)
        _park_auto_rebase_failure(
            gh, issue, state,
            message=(
                f"{config.HITL_MENTIONS} crash recovery for PR "
                f"#{pr_number} could not safely finalize: {detail} "
                f"Local HEAD has been reset to the pre-rebase SHA "
                f"`{pending_pre_rebase_sha[:8]}` so the worktree "
                "matches the (last-known) remote PR head -- the "
                "issue is parked so the same-tick stage handlers do "
                "NOT run against a SHA the PR may not carry. Reply "
                "on this issue with anything once the underlying "
                "problem is fixed and the orchestrator will re-"
                "attempt the auto rebase on the next polling tick."
            ),
            reason="auto_base_rebase_push_failed",
        )
        return True

    fetch_branch = _authed_fetch(
        spec,
        f"+refs/heads/{branch}:refs/remotes/{spec.remote_name}/{branch}",
        cwd=worktree,
    )
    if fetch_branch.returncode != 0:
        log.warning(
            "issue=#%d auto-rebase recovery fetch of %s/%s failed: %s; "
            "aborting recovery and parking awaiting human",
            issue.number, spec.remote_name, branch,
            (fetch_branch.stderr or "").strip(),
        )
        return _abort_recovery_unverified(
            f"the fetch of `{spec.remote_name}/{branch}` needed to "
            f"verify the recovered SHA against the remote PR head "
            f"failed (`{(fetch_branch.stderr or '').strip()[:120]}`)."
        )

    local_head = _head_sha(worktree) or ""

    if local_head and local_head == pending_pre_rebase_sha:
        # Case 1: HEAD never moved past the pre-rebase SHA (or was
        # reset back). Clear the flag and let the caller's normal
        # flow re-attempt the rebase.
        state.set("pending_auto_base_rebase_push_sha", None)
        gh.write_pinned_state(issue, state)
        log.info(
            "issue=#%d auto-rebase recovery: local HEAD matches pre-"
            "rebase SHA `%s`; clearing flag and falling through to "
            "the normal rebase flow",
            issue.number, pending_pre_rebase_sha[:8],
        )
        return False

    # Read the freshly-fetched remote PR head SHA directly. We do NOT
    # rely on `_branch_ahead_behind` alone for the case-2 finalize
    # decision: that helper returns `(0, 0)` on git errors AND on the
    # legitimate "in sync" case, which is indistinguishable. A
    # case-2 finalize relabels to `validating` and clears the
    # recovery anchor, so silently treating a git error as proof
    # that the push landed would route the issue to validating
    # against an unpublished SHA. `rev-parse` makes the comparison
    # source explicit: an unreadable remote ref aborts recovery
    # this tick AND parks (the same-tick handler dispatch would
    # otherwise run on a local HEAD that may not be on the PR), and
    # the case-2 finalize requires positive `local_head ==
    # remote_pr_head`.
    remote_ref = f"refs/remotes/{spec.remote_name}/{branch}"
    remote_head_r = _git_hardened("rev-parse", remote_ref, cwd=worktree)
    if remote_head_r.returncode != 0:
        log.warning(
            "issue=#%d auto-rebase recovery: rev-parse of %s failed "
            "after fetch: %s; aborting recovery and parking awaiting human",
            issue.number, remote_ref, (remote_head_r.stderr or "").strip(),
        )
        return _abort_recovery_unverified(
            f"`git rev-parse {remote_ref}` failed after the fetch "
            f"(`{(remote_head_r.stderr or '').strip()[:120]}`), so "
            "the remote PR head SHA needed for the equality check "
            "could not be read."
        )
    remote_pr_head = (remote_head_r.stdout or "").strip()
    if not remote_pr_head:
        log.warning(
            "issue=#%d auto-rebase recovery: rev-parse of %s returned "
            "no SHA; aborting recovery and parking awaiting human",
            issue.number, remote_ref,
        )
        return _abort_recovery_unverified(
            f"`git rev-parse {remote_ref}` returned no SHA after the "
            "fetch, so the remote PR head SHA needed for the "
            "equality check could not be read."
        )

    if local_head and local_head == remote_pr_head:
        # Case 2: local HEAD == remote PR head, verified by an explicit
        # rev-parse against the freshly-fetched remote ref. The push
        # from the prior tick landed; the recovery only needs to
        # clear the anchor, reset stale approval state, post a
        # notice, and emit the audit event.
        #
        # If `behind == 0` against the freshly-fetched base, the
        # recovered head is current -- finalize the relabel and
        # return True. If `behind > 0`, base advanced again since
        # the interrupted rebase, so the recovered head is still
        # behind base. Write the recovery state (so a follow-up
        # crash retains progress) and fall through (`return False`)
        # so the caller's normal rebase + push flow can rebase the
        # recovered head onto the newer base and push again -- the
        # final label flip then fires in the normal flow. Without
        # this, the same-tick `validating` handler would run the
        # reviewer on a PR that is still behind base.
        if unparking_consumed_max is not None:
            # The recovery has confirmed the recovered head is on
            # the PR, so commit the operator's "retry" unpark
            # atomically with this success state write. Even on the
            # behind>0 fall-through the recovered head IS on the
            # PR -- the only remaining work is rebasing once more
            # against the newer base, which the normal flow handles.
            state.set("last_action_comment_id", unparking_consumed_max)
            state.set("awaiting_human", False)
            state.set("park_reason", None)
        state.set("pending_auto_base_rebase_push_sha", None)
        state.set("review_round", 0)
        try:
            _post_pr_comment(
                gh, pr_number, state,
                f":mag: Recovered an interrupted auto-rebase for PR "
                f"#{pr_number}; the new head `{local_head[:8]}` was "
                f"already published before the orchestrator restart."
                + (
                    f" Routing `{label}` -> `validating` so the "
                    "reviewer re-runs against the rewritten branch."
                    if behind == 0
                    else f" Base advanced again by {behind} commit(s)"
                    f" since the interrupted rebase; rebasing once "
                    "more before routing to `validating`."
                ),
            )
        except Exception:
            log.exception(
                "issue=#%s could not post auto-rebase recovery notice "
                "to PR #%s", issue.number, pr_number,
            )
        gh.emit_event(
            "base_rebased",
            issue_number=issue.number,
            stage=label,
            pr_number=pr_number,
            sha=local_head,
            method="crash_recovery_relabel_only",
            review_round=0,
            retry_count=state.get("retry_count"),
        )
        if behind == 0:
            log.info(
                "issue=#%d auto-rebase recovery: push completed on a "
                "prior tick; routing %r -> validating",
                issue.number, label,
            )
            gh.set_workflow_label(issue, "validating")
            gh.write_pinned_state(issue, state)
            return True
        gh.write_pinned_state(issue, state)
        log.info(
            "issue=#%d auto-rebase recovery: recovered head `%s` is "
            "still %d commit(s) behind %s/%s; falling through to the "
            "normal rebase + push flow",
            issue.number, local_head[:8], behind,
            spec.remote_name, spec.base_branch,
        )
        return False

    # `local_head != remote_pr_head` here. Use `_branch_ahead_behind`
    # to distinguish "push pending" (HEAD ahead of remote PR head,
    # case 3) from "diverged" (HEAD behind / both ahead-and-behind,
    # case 4). On a `_branch_ahead_behind` git error we get `(0, 0)`,
    # which we have already ruled out (via the SHA inequality) is
    # the legitimate in-sync state -- so a `(0, 0)` here means the
    # remote-tracking ref we just fetched is unexpectedly missing.
    # Bail rather than guess.
    ahead, behind_pr = _branch_ahead_behind(spec, worktree, branch)
    if ahead == 0 and behind_pr == 0:
        log.warning(
            "issue=#%d auto-rebase recovery: local HEAD (`%s`) differs "
            "from remote PR head (`%s`) but `_branch_ahead_behind` "
            "returned `(0, 0)`; aborting recovery and parking awaiting "
            "human", issue.number, local_head[:8], remote_pr_head[:8],
        )
        return _abort_recovery_unverified(
            f"local HEAD `{local_head[:8]}` differs from remote PR "
            f"head `{remote_pr_head[:8]}` but `_branch_ahead_behind` "
            "returned `(0, 0)`, which means the remote-tracking ref "
            "we just fetched is unexpectedly missing -- the path the "
            "recovery would take next cannot be determined safely."
        )

    if behind_pr > 0:
        # Case 4: diverged from remote PR head. Reset HEAD back to
        # the pre-rebase SHA and park.
        reset = _git_hardened(
            "reset", "--hard", pending_pre_rebase_sha, cwd=worktree,
        )
        if reset.returncode != 0:
            log.error(
                "issue=#%d auto-rebase recovery: reset to pre-rebase "
                "SHA `%s` failed: %s",
                issue.number, pending_pre_rebase_sha[:8],
                (reset.stderr or "").strip(),
            )
        state.set("pending_auto_base_rebase_push_sha", None)
        _park_auto_rebase_failure(
            gh, issue, state,
            message=(
                f"{config.HITL_MENTIONS} crash recovery for PR "
                f"#{pr_number}: local worktree (`{local_head[:8]}`) "
                f"is {ahead} ahead and {behind_pr} behind remote "
                f"`{spec.remote_name}/{branch}` -- the remote PR "
                "branch was updated out-of-band during the interrupted "
                "auto rebase. HEAD has been reset to the pre-rebase "
                f"SHA `{pending_pre_rebase_sha[:8]}`. Investigate the "
                "remote PR head and reply on this issue with anything "
                "once the divergence is reconciled."
            ),
            reason="auto_base_rebase_push_failed",
        )
        return True

    # Case 3: local HEAD ahead of remote PR head. Push didn't go
    # through on the prior tick; try again with the original lease.
    dirty = _worktree_dirty_files(worktree)
    if dirty:
        reset = _git_hardened(
            "reset", "--hard", pending_pre_rebase_sha, cwd=worktree,
        )
        if reset.returncode != 0:
            log.error(
                "issue=#%d auto-rebase recovery: dirty-tree reset "
                "failed: %s", issue.number, (reset.stderr or "").strip(),
            )
        clean = _git_hardened("clean", "-fd", cwd=worktree)
        if clean.returncode != 0:
            log.error(
                "issue=#%d auto-rebase recovery: dirty-tree clean "
                "failed: %s", issue.number, (clean.stderr or "").strip(),
            )
        state.set("pending_auto_base_rebase_push_sha", None)
        _park_auto_rebase_failure(
            gh, issue, state,
            message=(
                f"{config.HITL_MENTIONS} crash recovery for PR "
                f"#{pr_number}: the rebased worktree (recovered from "
                f"a prior tick, HEAD `{local_head[:8]}`) carries "
                f"{len(dirty)} uncommitted change(s). HEAD has been "
                f"reset to the pre-rebase SHA `{pending_pre_rebase_sha[:8]}` "
                "and untracked files cleaned (use `git reflog` if you "
                "need the discarded edits). Investigate, then reply on "
                "this issue with anything to retry."
            ),
            reason="auto_base_rebase_dirty",
        )
        return True

    if not _push_branch(
        spec, worktree, branch, force_with_lease=pending_pre_rebase_sha,
    ):
        reset = _git_hardened(
            "reset", "--hard", pending_pre_rebase_sha, cwd=worktree,
        )
        if reset.returncode != 0:
            log.error(
                "issue=#%d auto-rebase recovery: push-failure reset "
                "failed: %s", issue.number, (reset.stderr or "").strip(),
            )
        state.set("pending_auto_base_rebase_push_sha", None)
        _park_auto_rebase_failure(
            gh, issue, state,
            message=(
                f"{config.HITL_MENTIONS} crash recovery for PR "
                f"#{pr_number}: `--force-with-lease` push of the "
                f"recovered rebase (`{local_head[:8]}`, lease against "
                f"`{pending_pre_rebase_sha[:8]}`) failed. HEAD has been "
                "reset to the pre-rebase SHA. Most likely the remote "
                "PR branch was updated out-of-band; investigate and "
                "reply on this issue with anything to retry."
            ),
            reason="auto_base_rebase_push_failed",
        )
        return True

    # Recovery push succeeded. Same `behind`-aware split as case 2:
    # if base did not advance since the interrupted rebase, finalize
    # the relabel and return True; if base advanced, write the
    # recovery state and fall through so the caller's normal rebase
    # + push flow rebases the just-pushed recovered head onto the
    # newer base before routing to `validating`.
    if unparking_consumed_max is not None:
        # The push went through, so the operator's "retry" goal is
        # satisfied. Commit the unpark atomically with this state
        # write.
        state.set("last_action_comment_id", unparking_consumed_max)
        state.set("awaiting_human", False)
        state.set("park_reason", None)
    state.set("pending_auto_base_rebase_push_sha", None)
    state.set("review_round", 0)
    try:
        _post_pr_comment(
            gh, pr_number, state,
            f":mag: Recovered an interrupted auto-rebase for PR "
            f"#{pr_number}; pushed the recovered head "
            f"`{local_head[:8]}`."
            + (
                f" Routing `{label}` -> `validating`."
                if behind == 0
                else f" Base advanced again by {behind} commit(s) "
                "since the interrupted rebase; rebasing once more "
                "before routing to `validating`."
            ),
        )
    except Exception:
        log.exception(
            "issue=#%s could not post auto-rebase recovery notice to "
            "PR #%s", issue.number, pr_number,
        )
    gh.emit_event(
        "base_rebased",
        issue_number=issue.number,
        stage=label,
        pr_number=pr_number,
        sha=local_head,
        method="crash_recovery_pushed",
        review_round=0,
        retry_count=state.get("retry_count"),
    )
    if behind == 0:
        log.info(
            "issue=#%d auto-rebase recovery: pushed recovered head %s; "
            "routing %r -> validating",
            issue.number, local_head[:8], label,
        )
        gh.set_workflow_label(issue, "validating")
        gh.write_pinned_state(issue, state)
        return True
    gh.write_pinned_state(issue, state)
    log.info(
        "issue=#%d auto-rebase recovery: pushed recovered head `%s` "
        "but it is still %d commit(s) behind %s/%s; falling through "
        "to the normal rebase + push flow",
        issue.number, local_head[:8], behind,
        spec.remote_name, spec.base_branch,
    )
    return False


def _sync_worktree_with_base(
    gh: GitHubClient, spec: RepoSpec, worktree: Path, issue_number: int,
) -> None:
    """Bring a single per-issue worktree up to date with `origin/<base>`.

    Pre-PR: rebase onto `origin/<base>` directly. PR-having + behind
    base + label in {validating, documenting, in_review, fixing}: hand
    off to `_sync_pr_worktree_to_base`, which attempts the rebase
    locally, pushes (force-with-lease pinned to the pre-rebase SHA),
    resets `review_round`, and relabels to `validating` on a clean
    rebase. Only when that rebase leaves conflicted files does the
    helper relabel to `resolving_conflict` so the existing handler
    drives the dev agent to resolve them. Skips a dirty worktree
    or a worktree already up to date (no pre-PR rebase attempted, no PR
    rebase fired). On a pre-PR content conflict, aborts the rebase so
    the worktree stays on its pre-rebase SHA -- conflict resolution
    for pre-PR worktrees still lives in `_handle_resolving_conflict`,
    reached only via an operator relabel until the issue earns a PR.
    """
    try:
        issue = gh.get_issue(issue_number)
    except Exception:
        log.debug(
            "issue=#%d not retrievable; skipping base sync", issue_number,
        )
        return
    if issue_has_label(issue, BACKLOG_LABEL):
        # Match the dispatcher's hard-skip: `backlog` means "the orchestrator
        # should not touch this issue at all", so refresh must not rebase
        # base, post a PR comment, or detour the issue to
        # `resolving_conflict` before `_process_issue` would have skipped it.
        log.debug(
            "issue=#%d has %r; skipping base sync",
            issue_number, BACKLOG_LABEL,
        )
        return
    if issue_has_label(issue, BASE_SYNC_HOLD_LABEL):
        log.debug(
            "issue=#%d has %r; skipping base sync",
            issue_number, BASE_SYNC_HOLD_LABEL,
        )
        return
    # `question`-labeled issues are read-only: the question agent
    # must not commit, and `_handle_question` already tears down the
    # per-issue worktree on every safe exit. The only worktrees that
    # survive across ticks under this label are the unsafe-park
    # cases (`question_commits`, `question_dirty`, `question_timeout`)
    # where the operator is supposed to inspect what the agent did
    # before resetting; merging `origin/<base>` over that inspection
    # state would mask it. Skip base sync entirely while the label
    # is `question` so the read-only contract holds even if the
    # handler ever leaves the worktree on disk unexpectedly.
    if issue_has_label(issue, "question"):
        log.debug(
            "issue=#%d has 'question' label; skipping base sync "
            "(read-only stage)", issue_number,
        )
        return
    state = gh.read_pinned_state(issue)
    pr_number = state.get("pr_number")

    # Pre-PR worktrees hard-skip on dirty: no remote anchor to recover
    # towards, and no crash-recovery flag to honor. PR-having worktrees
    # defer the dirty check into `_sync_pr_worktree_to_base` so a crash
    # mid-rebase that left BOTH `pending_auto_base_rebase_push_sha` set
    # AND uncommitted edits on the worktree still reaches
    # `_recover_pending_auto_base_rebase`'s reset+clean+park path
    # instead of being silently skipped here (which would leave the
    # same-tick stage handler reading a local HEAD that is NOT on the
    # PR).
    if pr_number is None and _worktree_dirty_files(worktree):
        log.debug(
            "issue=#%d skipping base sync: worktree has uncommitted changes",
            issue_number,
        )
        return

    base_ref = f"{spec.remote_name}/{spec.base_branch}"
    behind_r = _git(
        "rev-list", "--count", f"HEAD..{base_ref}", cwd=worktree,
    )
    if behind_r.returncode != 0:
        log.debug(
            "issue=#%d skipping base sync: rev-list failed: %s",
            issue_number, (behind_r.stderr or "").strip(),
        )
        return
    try:
        behind = int((behind_r.stdout or "0").strip() or "0")
    except ValueError:
        return

    if pr_number is not None:
        # PR-having worktrees route here even when `behind == 0` -- the
        # crash-recovery check inside `_sync_pr_worktree_to_base` keys
        # off the pinned `pending_auto_base_rebase_push_sha` anchor,
        # which is set on a normal tick BEFORE the rebase. A crash
        # between rebase and push (or between push and relabel/state
        # write) leaves local HEAD ahead of (or matching) the remote
        # PR head AT a SHA that contains base, so the rev-list
        # `HEAD..<base>` reports 0 even though the PR has not received
        # the rewrite yet. Skipping here on `behind == 0` would leave
        # validating reviewing a local SHA not on the PR (scenario 1)
        # or in_review / documenting / fixing parked on stale state
        # after a branch rewrite (scenario 2).
        _sync_pr_worktree_to_base(
            gh, spec, issue, state, worktree, int(pr_number), behind,
        )
        return

    if behind == 0:
        return

    succeeded, conflicted = _rebase_base_into_worktree(spec, worktree)
    if succeeded:
        log.info(
            "issue=#%d rebased worktree onto %s (was %d commit(s) behind)",
            issue_number, base_ref, behind,
        )
        return

    abort = _git_hardened("rebase", "--abort", cwd=worktree)
    if abort.returncode != 0:
        log.warning(
            "issue=#%d base rebase failed and abort failed: %s",
            issue_number, (abort.stderr or "").strip(),
        )
    if conflicted:
        log.info(
            "issue=#%d base rebase has %d conflict(s); aborted -- "
            "resolving_conflict will handle it once a PR exists",
            issue_number, len(conflicted),
        )
    else:
        log.warning(
            "issue=#%d base rebase failed without conflicted files; aborted",
            issue_number,
        )


def _sync_pr_worktree_to_base(
    gh: GitHubClient,
    spec: RepoSpec,
    issue: Issue,
    state: PinnedState,
    worktree: Path,
    pr_number: int,
    behind: int,
) -> None:
    """Bring a behind-base PR-having issue back to merge-ready.

    On a clean rebase: rebase the worktree onto `origin/<base>`, push
    with `--force-with-lease` pinned to the pre-rebase SHA (so a
    concurrent foreign update on the remote PR branch rejects the
    push instead of being clobbered), reset `review_round` to 0, post
    an informational PR notice, and relabel to `validating` so the
    reviewer re-runs against the rewritten head. Docs do not run on
    this exit -- the single docs pass runs after the next reviewer
    approval via the final-docs handoff to `documenting` in
    `_handle_validating`. This is the only safe pattern for PR-having
    worktrees, since a local-only rebase without a push would diverge
    local HEAD from `pr.head.sha` and break every downstream gate
    that compares the two.

    Only when the rebase actually leaves conflicted files do we
    relabel to `resolving_conflict`: the handler then drives the dev
    agent to resolve the conflict, pushes, and bounces back to
    `validating`. This reserves the `resolving_conflict` label for
    real rebase conflicts (or an operator manual application) and
    keeps the merely-behind-base case off it -- the label no longer
    flips on a clean sibling-PR merge that the orchestrator can
    auto-rebase. `_handle_in_review` is also permanently manual-
    merge-only and just parks awaiting human attention on an
    unmergeable PR.

    Skipped (label stays put, no PR notice, no push) when:

    * The label is not one the refresh drives (only `validating` /
      `documenting` / `in_review` / `fixing`); `resolving_conflict`
      itself is also skipped because the handler runs this tick anyway
      and will do the rebase regardless.

    * `awaiting_human=True`. The orchestrator already parked the issue
      and an attempted auto-rebase here would either re-open work that
      the human is meant to resolve or undermine the
      `MAX_REVIEW_ROUNDS` / `MAX_CONFLICT_ROUNDS` caps that exist
      precisely to require human intervention after repeated failures.

    * The issue has `hold_base_sync`, an explicit operator hold for
      series work where base should be integrated once after the
      prerequisite PRs land, not after every intermediate base advance.

    * The PR is no longer open. A merged PR advances `origin/<base>`,
      so the still-validating / still-in_review / still-fixing
      worktree pointed at the now-stale branch is naturally behind
      base; without this gate the refresh would push, post an
      "auto-rebased" notice, and relabel to `validating` on a PR the
      next handler call would finalize to `done`. Same for closed-
      without-merge if base advanced concurrently (handler would
      finalize to `rejected`). Leave terminal PR state to the
      existing stage logic. A `gh.get_pr` failure is treated as
      "leave it alone" -- the handler can retry on the next tick from
      a stable label rather than racing a half-known PR state from
      refresh.

    The watermark bump in `_handle_in_review`'s analogous unmergeable
    detour is deliberately NOT replicated here. That bump is safe
    in_review-side because `_handle_in_review` has already scanned new
    comments before the relabel (anything past the watermark has been
    consumed by the fix-loop or filtered as orchestrator-authored).
    The refresh-time flow runs BEFORE any handler scans comments, so
    `latest_comment_id` may include unread human "do not merge" /
    fix-request comments; advancing the watermark here would silently
    mark them consumed and later validation / merge would skip them.
    The orchestrator's own PR notice we just posted is filtered out
    via `orchestrator_comment_ids` on the next `_handle_in_review`
    scan, so leaving the watermark alone does not cause the
    orchestrator to "see" its own message as fresh feedback. The
    `pending_fix_*` bookmarks recorded by an `in_review` -> `fixing`
    route are similarly left untouched: the next handler that resumes
    that route still finds them, and a stale bookmark on a now-
    `validating` issue is harmless (the reviewer pass clears it
    naturally when it next bounces to `fixing`).

    Dirty worktrees abort the push: a pre-existing uncommitted edit
    would otherwise be force-pushed alongside the rebase result, and
    the validating reviewer would then vote on a tree that does NOT
    match the PR head. Mirrors `_handle_resolving_conflict`'s refuse-
    to-publish-an-incomplete-branch rule. A push failure (the lease
    rejection most commonly surfaces a diverged or crash-recovery
    branch) leaves the label alone too; the next tick can retry once
    the underlying divergence is reconciled.
    """
    # Read once: every early return below also needs to clear a stale
    # recovery anchor so a flag set on a prior tick cannot survive a
    # manual operator relabel / hold / terminal PR transition and
    # later trigger bogus recovery when the issue returns to a
    # refresh-driven label.
    pending_pre_rebase_sha = state.get("pending_auto_base_rebase_push_sha")

    label = gh.workflow_label(issue)
    if label not in _PR_REFRESH_DETOUR_LABELS:
        if pending_pre_rebase_sha:
            # Operator manually relabeled (typically to
            # `resolving_conflict`) while a recovery anchor from an
            # interrupted auto-rebase was still pinned. Without this
            # branch the stale flag survives the manual conflict
            # workflow and -- once the operator relabels back to a
            # refresh-driven label -- triggers bogus recovery/reset
            # behavior against a pre-rebase SHA that no longer
            # matches reality. Hand off to the recovery helper's
            # label-not-in-set cleanup branch so the clear is logged
            # uniformly with every other clear site.
            _recover_pending_auto_base_rebase(
                gh, spec, issue, state, worktree,
                pr_number=pr_number,
                label=label,
                pending_pre_rebase_sha=str(pending_pre_rebase_sha),
            )
        log.debug(
            "issue=#%d behind %s/%s by %d but label=%r; not auto-rebasing",
            issue.number, spec.remote_name, spec.base_branch, behind, label,
        )
        return

    # `unparking_consumed_max` captures the auto-rebase-park retry
    # intent so the eventual commit point (recovery success, normal-
    # flow anchor set) can clear `awaiting_human` / `park_reason` and
    # advance `last_action_comment_id` past the operator's reply
    # ATOMICALLY with the state write that actually publishes the
    # rebase. Until then it stays a local var; in-memory state still
    # carries `awaiting_human=True`. The downstream gates
    # (`hold_base_sync`, PR fetch failure, PR-state terminal, dirty,
    # `behind == 0`, recovery's case-1 fall-through) can therefore
    # early-return WITHOUT having silently dropped the park: on-disk
    # `awaiting_human` is untouched until the rebase actually
    # commits, the operator's comment is still ahead of the
    # watermark (the next refresh tick rediscovers it), and the
    # same-tick stage handler dispatch still sees `awaiting_human=True`
    # so its own auto-rebase-park gate short-circuits the comment
    # processing.
    unparking_consumed_max: Optional[int] = None
    if state.get("awaiting_human"):
        park_reason = state.get("park_reason")
        if park_reason not in _AUTO_REBASE_PARK_REASONS:
            # Park belongs to a stage handler (`unmergeable`,
            # `agent_question`, `review_cap`, ...) -- not ours to clear.
            log.debug(
                "issue=#%d behind %s/%s by %d but awaiting_human=True "
                "with park_reason=%r; leaving park intact rather than "
                "auto-rebasing",
                issue.number, spec.remote_name, spec.base_branch, behind,
                park_reason,
            )
            return
        # Auto-rebase park: a new human comment on the issue thread is
        # the "retry now" signal. Without a new comment the human has
        # not acknowledged the message yet, so the park stays.
        last_action_id = state.get("last_action_comment_id")
        new_comments = gh.comments_after(issue, last_action_id)
        if not new_comments:
            log.debug(
                "issue=#%d behind %s/%s by %d, parked on %r with no new "
                "human comment; staying parked",
                issue.number, spec.remote_name, spec.base_branch, behind,
                park_reason,
            )
            return
        unparking_consumed_max = max(c.id for c in new_comments)
        log.info(
            "issue=#%d parked on %r had a new human comment; will clear "
            "the park if a retry is actually attempted this tick (gates "
            "that early-return preserve the park on disk so the "
            "operator's reply is not silently consumed)",
            issue.number, park_reason,
        )

    if issue_has_label(issue, BASE_SYNC_HOLD_LABEL):
        # `hold_base_sync` deliberately leaves the recovery anchor in
        # place: the operator may want to remove the hold later, at
        # which point the next refresh runs the recovery against the
        # original pre-rebase SHA. Clearing here would erase that
        # signal.
        log.debug(
            "issue=#%d behind %s/%s by %d but has %r; not auto-rebasing",
            issue.number, spec.remote_name, spec.base_branch, behind,
            BASE_SYNC_HOLD_LABEL,
        )
        return

    try:
        pr = gh.get_pr(pr_number)
    except Exception:
        log.debug(
            "issue=#%d could not fetch PR #%d for refresh rebase; "
            "leaving label alone, handler will retry next tick",
            issue.number, pr_number,
        )
        return
    pr_status = gh.pr_state(pr)
    if pr_status != "open":
        # Merged / closed PR: the next handler call finalizes to done /
        # rejected. The base advance that put us "behind" is exactly the
        # merge that closed this PR -- there is nothing to auto-resolve.
        if pending_pre_rebase_sha:
            # Drop the recovery anchor before the terminal finalize:
            # the PR is gone (merged or closed), so there is no
            # rewritten branch to reconcile -- the anchor would only
            # confuse a later refresh if the issue is somehow
            # re-opened.
            state.set("pending_auto_base_rebase_push_sha", None)
            gh.write_pinned_state(issue, state)
            log.info(
                "issue=#%d PR #%d is %s and a recovery anchor was "
                "pinned; clearing the stale flag",
                issue.number, pr_number, pr_status,
            )
        log.debug(
            "issue=#%d PR #%d is %s; not auto-rebasing (handler will finalize)",
            issue.number, pr_number, pr_status,
        )
        return

    # Crash recovery: a prior tick set `pending_auto_base_rebase_push_sha`
    # to the pre-rebase SHA before attempting the rebase + push. If the
    # orchestrator died between then and the post-push relabel /
    # state-write, the worktree is now in one of:
    #   * local HEAD ahead of remote PR head (rebase done, push not),
    #   * local HEAD == remote PR head (push done, relabel not),
    #   * local HEAD == pending SHA (rebase no-op or reverted).
    # Without recovery the next refresh's `behind == 0` check would
    # skip the worktree, leaving the same-tick handler reviewing a SHA
    # that is NOT on the PR (scenario 1) or running on stale label /
    # `review_round` (scenario 2). `_recover_pending_auto_base_rebase`
    # finalizes / re-pushes / parks as appropriate.
    if pending_pre_rebase_sha:
        if _recover_pending_auto_base_rebase(
            gh, spec, issue, state, worktree,
            pr_number=pr_number,
            label=label,
            pending_pre_rebase_sha=str(pending_pre_rebase_sha),
            behind=behind,
            unparking_consumed_max=unparking_consumed_max,
        ):
            return
        # Recovery cleared a stale flag (case 1) without finalizing,
        # OR finalized cases 2 / 3 but base has advanced further so
        # the recovered head is still behind base. Fall through so
        # the normal flow can re-attempt the rebase on this same
        # tick if the worktree is still behind base. The recovery's
        # case-2/3 fall-through has already committed any pending
        # unpark on its state write; case 1 deliberately did NOT
        # touch the park (since it only cleared the anchor and made
        # no real progress), so the normal-flow anchor set below is
        # what commits the unpark on a case-1 path. The dirty /
        # behind==0 early-returns below leave the park on disk.
        if unparking_consumed_max is not None:
            # Recovery's case-2/3 paths advanced the watermark and
            # cleared the park as part of their state write; clear
            # the local intent so the normal-flow anchor set does
            # not double-write the same fields. The case-1 path
            # leaves `awaiting_human` set in pinned state, so the
            # local var still reflects work to do.
            if not state.get("awaiting_human"):
                unparking_consumed_max = None

    # Dirty-tree skip lives HERE (not in the outer
    # `_sync_worktree_with_base`) so the crash-recovery branch above
    # gets a chance to reset+clean+park a worktree that was left
    # dirty mid-rebase. Cases 2-4 of `_recover_pending_auto_base_rebase`
    # all handle dirty internally (case 3 explicitly checks; cases 2
    # and 4 don't push or care). Only case 1 (HEAD == anchor)
    # returned False and falls through here -- a dirty worktree at
    # the pre-rebase SHA is a pre-existing "dirty during normal flow"
    # condition, so skip exactly like the outer dirty hard-skip
    # would have.
    if _worktree_dirty_files(worktree):
        log.debug(
            "issue=#%d skipping base sync: worktree has uncommitted changes",
            issue.number,
        )
        return

    if behind == 0:
        # No rebase to attempt. (After the crash-recovery check so a
        # scenario-2 finalize still fires when `behind == 0`.)
        return

    before_sha = _head_sha(worktree) or ""
    if not before_sha:
        # Fail closed: an unreadable pre-rebase HEAD would weaken the
        # whole rebase + push flow. The crash-recovery anchor would
        # land as `None` (no signal for the next tick to recover from),
        # `_push_branch(force_with_lease=None)` would fall back to an
        # `ls-remote` lease that does NOT match the pre-rebase tip we
        # never knew, and the post-rebase `not after_sha or after_sha
        # == before_sha` no-op check would silently treat a moved HEAD
        # as unchanged. Park awaiting human; the operator can
        # investigate why `git rev-parse HEAD` is failing.
        log.error(
            "issue=#%d cannot read local HEAD before auto base rebase; "
            "parking awaiting human (no rebase attempted)",
            issue.number,
        )
        _park_auto_rebase_failure(
            gh, issue, state,
            message=(
                f"{config.HITL_MENTIONS} PR #{pr_number} is {behind} "
                f"commit(s) behind `{spec.remote_name}/{spec.base_branch}`, "
                "but the orchestrator could not read local `HEAD` on "
                "the per-issue worktree before attempting the auto "
                "rebase. Force-with-lease pushes and the crash-recovery "
                "anchor both require a known pre-rebase SHA, so the "
                "rebase was skipped. Inspect the worktree's git state "
                "and reply on this issue with anything to retry."
            ),
            reason="auto_base_rebase_failed",
        )
        return
    # Set the crash-recovery anchor BEFORE the rebase. The window
    # between this state write and `_rebase_base_into_worktree`
    # returning is the only place a crash leaves NO recovery signal;
    # everything between this point and the post-push state write is
    # covered by `_recover_pending_auto_base_rebase` keying off the
    # flag. This is also the "we have committed to retrying" point
    # for the deferred auto-rebase-park unpark above: clear the
    # park / advance the watermark in the SAME state write so the
    # operator's reply is consumed atomically with the rebase
    # attempt that actually responds to it.
    if unparking_consumed_max is not None:
        state.set("last_action_comment_id", unparking_consumed_max)
        state.set("awaiting_human", False)
        state.set("park_reason", None)
    state.set("pending_auto_base_rebase_push_sha", before_sha)
    gh.write_pinned_state(issue, state)

    succeeded, conflicted_files = _rebase_base_into_worktree(spec, worktree)

    if not succeeded:
        # Abort the rebase so the worktree returns to its pre-rebase SHA
        # before we either route to `resolving_conflict` (which will
        # re-attempt the rebase itself) or leave the label alone.
        abort = _git_hardened("rebase", "--abort", cwd=worktree)
        if abort.returncode != 0:
            log.warning(
                "issue=#%d base rebase failed and abort failed: %s",
                issue.number, (abort.stderr or "").strip(),
            )
        # No rebased SHA was produced, so the crash-recovery anchor
        # has nothing to recover from. Clear it before the routing /
        # park writes pinned state.
        state.set("pending_auto_base_rebase_push_sha", None)
        if conflicted_files:
            _route_pr_worktree_to_resolving_conflict(
                gh, spec, issue, state, pr_number,
                label=label,
                behind=behind,
                conflicted_files=conflicted_files,
                pr_head_sha=getattr(pr.head, "sha", None) or None,
            )
            return
        # Rebase failed without conflicted files: the worktree is now
        # back at `before_sha` (the abort restored it), but the
        # underlying failure (planted hook, permissions, smudge filter,
        # etc.) is not something the next tick will magically resolve.
        # Park awaiting human so the same-tick `_handle_in_review` /
        # `_handle_fixing` / `_handle_validating` / `_handle_documenting`
        # dispatch does NOT continue on a still-behind-base PR -- the
        # in_review HITL ready-ping could otherwise advertise a behind-
        # base PR as ready for human merge if GitHub still reports it
        # mergeable.
        log.warning(
            "issue=#%d base rebase failed without conflicted files; "
            "parking awaiting human (refresh-only recovery on a new "
            "issue comment)",
            issue.number,
        )
        _park_auto_rebase_failure(
            gh, issue, state,
            message=(
                f"{config.HITL_MENTIONS} PR #{pr_number} is {behind} "
                f"commit(s) behind `{spec.remote_name}/{spec.base_branch}` "
                "and the auto rebase failed for a non-conflict reason "
                "(planted hook, smudge filter, permissions, ...). The "
                "worktree was restored to the pre-rebase SHA via "
                "`git rebase --abort`. Investigate the worktree / hooks, "
                "then reply on this issue with anything once the "
                "underlying problem is fixed; the next polling tick will "
                "re-attempt the auto rebase."
            ),
            reason="auto_base_rebase_failed",
        )
        return

    after_sha = _head_sha(worktree)
    if not after_sha:
        # Fail closed: an unreadable post-rebase HEAD makes the rebased
        # SHA unknown. Treating it as a no-op (the previous behavior
        # of `not after_sha or after_sha == before_sha`) would clear
        # the crash-recovery anchor and leave the worktree on an
        # unknown SHA. Reset HEAD back to the pre-rebase SHA (the
        # rebase moved HEAD by some amount we can't measure, so the
        # only safe state is the known one) and park awaiting human.
        log.error(
            "issue=#%d cannot read local HEAD after auto base rebase; "
            "resetting to pre-rebase SHA and parking awaiting human",
            issue.number,
        )
        reset = _git_hardened(
            "reset", "--hard", before_sha, cwd=worktree,
        )
        if reset.returncode != 0:
            log.error(
                "issue=#%d unreadable post-rebase HEAD AND reset to %s "
                "failed: %s; worktree may be on an unknown SHA",
                issue.number, before_sha[:8],
                (reset.stderr or "").strip(),
            )
        state.set("pending_auto_base_rebase_push_sha", None)
        _park_auto_rebase_failure(
            gh, issue, state,
            message=(
                f"{config.HITL_MENTIONS} PR #{pr_number} is {behind} "
                f"commit(s) behind `{spec.remote_name}/{spec.base_branch}`. "
                "The auto rebase ran but the orchestrator could not "
                "read local `HEAD` afterwards. HEAD has been reset to "
                f"the pre-rebase SHA `{before_sha[:8]}` so the worktree "
                "still matches the remote PR head. Inspect the "
                "worktree's git state and reply on this issue with "
                "anything to retry."
            ),
            reason="auto_base_rebase_failed",
        )
        return
    if after_sha == before_sha:
        # Worktree was already up to date with `origin/<base>` (the
        # base-rebase replayed nothing). The behind-count came from a
        # stale remote-tracking ref or the issue tree happened to
        # already contain the base advance. Local HEAD is still at the
        # pre-rebase SHA = remote PR head, so the worktree side is
        # consistent. We can safely leave the label alone -- no relabel,
        # no park: the next handler runs on a (correct head, behind
        # base) state, which is a normal interim state during sibling
        # PRs landing.
        log.info(
            "issue=#%d base rebase was a no-op despite %d commit(s) "
            "behind %s/%s; leaving label alone",
            issue.number, behind, spec.remote_name, spec.base_branch,
        )
        # Clear the crash-recovery anchor: no rebase happened, so the
        # next tick should not enter recovery.
        state.set("pending_auto_base_rebase_push_sha", None)
        gh.write_pinned_state(issue, state)
        return

    dirty = _worktree_dirty_files(worktree)
    if dirty:
        # Dirty-after-clean-rebase: the rebase succeeded and moved
        # local HEAD forward, but uncommitted edits (likely from a
        # smudge filter or external race) appeared during the replay.
        # We can NOT publish this state (would force-push a SHA whose
        # tree does not match the committed content) and we can NOT
        # leave the rebased SHA on local HEAD either (the same-tick
        # handler dispatch would otherwise have validating /
        # documenting / in_review / fixing read a local HEAD that is
        # NOT on the PR). Reset HEAD and the working tree back to the
        # pre-rebase SHA -- the dirty files survive in the reflog if
        # the operator needs them -- and park awaiting human.
        log.warning(
            "issue=#%d worktree has %d uncommitted change(s) after "
            "auto base rebase; resetting HEAD and parking awaiting human",
            issue.number, len(dirty),
        )
        if before_sha:
            reset = _git_hardened(
                "reset", "--hard", before_sha, cwd=worktree,
            )
            if reset.returncode != 0:
                log.error(
                    "issue=#%d auto base rebase left worktree dirty AND "
                    "reset back to %s failed: %s",
                    issue.number, before_sha[:8],
                    (reset.stderr or "").strip(),
                )
            clean = _git_hardened("clean", "-fd", cwd=worktree)
            if clean.returncode != 0:
                log.error(
                    "issue=#%d auto base rebase dirty-cleanup "
                    "`git clean -fd` failed: %s",
                    issue.number, (clean.stderr or "").strip(),
                )
        # Reset above restored HEAD to before_sha, so there is no
        # rebased SHA left to recover from. Clear the anchor before
        # the park writes pinned state.
        state.set("pending_auto_base_rebase_push_sha", None)
        _park_auto_rebase_failure(
            gh, issue, state,
            message=(
                f"{config.HITL_MENTIONS} PR #{pr_number} is {behind} "
                f"commit(s) behind `{spec.remote_name}/{spec.base_branch}` "
                f"and the auto rebase landed cleanly but left "
                f"{len(dirty)} uncommitted change(s) on the worktree. "
                "Local HEAD has been reset to the pre-rebase SHA and "
                "untracked files cleaned (use `git reflog` if you need "
                "the discarded edits). Investigate the smudge filter / "
                "hook / external race that produced the dirty tree, "
                "then reply on this issue with anything to retry."
            ),
            reason="auto_base_rebase_dirty",
        )
        return

    branch = _branch_name(issue.number)
    if not _push_branch(
        spec, worktree, branch, force_with_lease=before_sha or None,
    ):
        # The lease check is what catches a diverged or crash-recovery
        # branch: the pre-rebase SHA only matches the live remote when
        # the worktree is in sync with the PR head, so a divergence
        # silently rejects the push instead of clobbering work.
        #
        # Step 1 -- reset local HEAD back to the pre-rebase SHA so the
        # worktree matches the still-stale remote PR head again.
        # Otherwise the rebased SHA stays on local HEAD while the
        # remote PR head is still stale, which breaks two downstream
        # contracts: (a) the next tick's behind check (HEAD vs
        # `origin/<base>`) reports `behind == 0` because the base
        # advance is now part of local HEAD, so the refresh never
        # retries; (b) the validating reviewer reads local HEAD, so it
        # would review a SHA that is NOT on the PR.
        if before_sha:
            reset = _git_hardened(
                "reset", "--hard", before_sha, cwd=worktree,
            )
            if reset.returncode != 0:
                log.error(
                    "issue=#%d auto base rebase push failed AND reset "
                    "of HEAD back to %s failed: %s; worktree may be "
                    "on a rebased SHA the remote PR does not have",
                    issue.number, before_sha[:8],
                    (reset.stderr or "").strip(),
                )
        # Step 2 -- park awaiting human. `tick()` runs the base refresh
        # BEFORE dispatching the same issue's handler, so simply
        # returning here would let `_handle_in_review` /
        # `_handle_fixing` / `_handle_validating` /
        # `_handle_documenting` process the issue this tick on a PR
        # head that is still behind base: the in_review ready-ping
        # could advertise a behind-base PR as ready for human merge if
        # GitHub still reports it mergeable, the reviewer / dev would
        # act on a stale base, and the lease failure that surfaced a
        # real divergence would never get an operator's attention.
        # Parking via `_park_auto_rebase_failure` sets
        # `awaiting_human=True` and posts a HITL message; every PR-
        # stage handler's awaiting-human gate then short-circuits the
        # issue this tick. The custom `park_reason` is what the
        # refresh recovery branch above keys off to clear the park on
        # the next human comment.
        #
        # Reset above restored HEAD to before_sha, so there is no
        # rebased SHA left to recover from. Clear the anchor before
        # the park writes pinned state.
        state.set("pending_auto_base_rebase_push_sha", None)
        _park_auto_rebase_failure(
            gh, issue, state,
            message=(
                f"{config.HITL_MENTIONS} PR #{pr_number} is "
                f"{behind} commit(s) behind "
                f"`{spec.remote_name}/{spec.base_branch}`; the orchestrator "
                "rebased the worktree cleanly but pushing the rewritten "
                f"branch (`--force-with-lease` against "
                f"`{(before_sha or '')[:8]}`) failed. Local HEAD has "
                "been reset to the pre-rebase SHA so the worktree still "
                "matches the remote PR head. Most likely the PR branch "
                f"was updated out-of-band; investigate the remote "
                f"`{branch}` and reply on this issue with anything once "
                "the branch is ready for the orchestrator to re-attempt "
                "the auto-rebase on the next polling tick."
            ),
            reason="auto_base_rebase_push_failed",
        )
        log.warning(
            "issue=#%d auto base rebase pushed nothing (lease rejection "
            "or push failure); local HEAD reset and issue parked awaiting "
            "human so the in_review / fixing / validating / documenting "
            "handlers do not process the issue on a behind-base PR head "
            "this tick",
            issue.number,
        )
        return

    try:
        _post_pr_comment(
            gh, pr_number, state,
            f":mag: PR was {behind} commit(s) behind "
            f"`{spec.remote_name}/{spec.base_branch}`; orchestrator "
            "auto-rebased the branch and re-pushed it. Routing "
            f"`{label}` -> `validating` so the reviewer re-runs "
            f"against the new head (`{after_sha[:8]}`).",
        )
    except Exception:
        log.exception(
            "issue=#%s could not post auto-rebase notice to PR #%s",
            issue.number, pr_number,
        )

    # Push succeeded: clear the crash-recovery anchor before the
    # remaining state writes so a crash anywhere from here on still
    # gets recovered cleanly (`_recover_pending_auto_base_rebase`'s
    # `ahead == 0` branch fires when the next tick sees local HEAD
    # == remote PR head with the anchor still set).
    state.set("pending_auto_base_rebase_push_sha", None)
    state.set("review_round", 0)

    log.info(
        "issue=#%d auto base rebase pushed %s/%s -> %s; routing %r -> "
        "validating",
        issue.number, spec.remote_name, branch, after_sha[:8], label,
    )
    gh.emit_event(
        "base_rebased",
        issue_number=issue.number,
        stage=label,
        pr_number=pr_number,
        sha=after_sha,
        method="auto_clean_rebase",
        review_round=int(state.get("review_round") or 0),
        retry_count=state.get("retry_count"),
    )
    gh.set_workflow_label(issue, "validating")
    gh.write_pinned_state(issue, state)


def _route_pr_worktree_to_resolving_conflict(
    gh: GitHubClient,
    spec: RepoSpec,
    issue: Issue,
    state: PinnedState,
    pr_number: int,
    *,
    label: str,
    behind: int,
    conflicted_files: list[str],
    pr_head_sha: Optional[str],
) -> None:
    """Relabel a PR-having issue to `resolving_conflict` for real conflicts.

    Called by `_sync_pr_worktree_to_base` when the auto-rebase left
    unresolved conflicted files. Seeds `conflict_round` only when
    absent (so a re-entry preserves the cap counter and a perpetually-
    stuck PR can't ping-pong indefinitely), posts a PR notice naming
    the conflicted files, emits the `conflict_round` "entered" audit
    event, and flips the workflow label so the existing
    `_handle_resolving_conflict` handler picks the work up on the
    same tick (the handler runs after the refresh in `tick()`).

    `pr_head_sha` is the remote PR head SHA at the time the rebase
    was attempted -- threaded in by the caller from the same
    `gh.get_pr(pr_number)` it uses for the PR-state gate -- so the
    emitted `conflict_round` `action="entered"` record carries the
    same `sha` field every other emit site populates
    (`docs/observability.md` documents it as part of the event shape).
    """
    # Match `_handle_in_review`'s seeding: only initialize `conflict_round`
    # when absent, so a re-entry preserves the cap counter and a
    # perpetually-stuck PR can't ping-pong between handlers indefinitely.
    if state.get("conflict_round") is None:
        state.set("conflict_round", 0)

    try:
        _post_pr_comment(
            gh, pr_number, state,
            f":mag: PR is {behind} commit(s) behind "
            f"`{spec.remote_name}/{spec.base_branch}` and the auto "
            f"rebase left {len(conflicted_files)} conflicted file(s); "
            "orchestrator is attempting auto-resolution via the dev "
            "agent (label: `resolving_conflict`).",
        )
    except Exception:
        log.exception(
            "issue=#%s could not post auto-rebase notice to PR #%s",
            issue.number, pr_number,
        )

    log.info(
        "issue=#%d behind %s/%s by %d commit(s) with %d conflicted "
        "file(s); routing %r -> resolving_conflict so the handler "
        "drives the dev agent",
        issue.number, spec.remote_name, spec.base_branch, behind,
        len(conflicted_files), label,
    )
    gh.emit_event(
        "conflict_round",
        issue_number=issue.number,
        stage=label,
        pr_number=pr_number,
        sha=pr_head_sha or None,
        action="entered",
        conflict_round=int(state.get("conflict_round") or 0),
        review_round=int(state.get("review_round") or 0),
        retry_count=state.get("retry_count"),
    )
    gh.set_workflow_label(issue, "resolving_conflict")
    gh.write_pinned_state(issue, state)
