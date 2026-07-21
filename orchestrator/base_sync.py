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
  trees, `backlog` / `paused`, and the `question` label.
* `_sync_pr_worktree_to_base` -- for a behind-base PR-having issue,
  attempt a local rebase + push (force-with-lease); on a clean rebase
  reset `review_round` and relabel to `validating`; only relabel to
  `resolving_conflict` when the rebase actually leaves conflicted files.

Imports the hardened git subprocess layer from `git_plumbing.py`, the
worktree-layout helpers from `worktree_lifecycle.py`, the worktree-
state probes (`_worktree_dirty_files`, `_head_sha`) from `verify.py`,
the branch-publication helpers (`_push_branch`) from `git_plumbing.py`,
and the PR-comment helper from `workflow_messages.py`. `worktrees.py`
re-exports its compatibility-facing imports from this module under their
original names. Existing imports and test patches that target
`orchestrator.worktrees` keep resolving those symbols; for example,
`_refresh_base_and_worktrees` remains importable there. The auto-rebase
context, decisions, and flow helpers remain private to this module. The
actual call graph lives here, so test patches that need to INTERCEPT a
call from inside
`_refresh_base_and_worktrees` / `_sync_worktree_with_base` /
`_sync_pr_worktree_to_base` should target this module (`base_sync`)
directly.

Helpers remain prefixed with `_` because they are module-internal
contracts -- the public surface (the dispatcher entry points and the
stage handlers they route to) still lives in `workflow.py` and
`orchestrator/stages/`.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Tuple

from github.Issue import Issue
from github.PullRequest import PullRequest

from orchestrator import config
from orchestrator.comment_trust import filter_trusted
from orchestrator.branch_publication import _branch_ahead_behind
from orchestrator.git_plumbing import (
    _authed_fetch,
    _authed_target_fetch,
    _git,
    _git_hardened,
    _push_branch,
)
from orchestrator.state_machine import WorkflowLabel
from orchestrator.github import (
    GitHubClient,
    PinnedState,
    hard_skip_control_label,
    issue_has_label,
)
from orchestrator.scheduler import IssueScheduler
from orchestrator.verify import _head_sha, _worktree_dirty_files
from orchestrator.workflow_messages import _post_pr_comment
from orchestrator.worktree_lifecycle import _repo_worktrees_root, _resolve_branch_name

log = logging.getLogger(__name__)


def _rebase_base_into_worktree(
    spec: config.RepoSpec, worktree: Path
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
    rebase_result = _git_hardened(
        "rebase",
        f"{spec.remote_name}/{spec.base_branch}", cwd=worktree,
    )
    if rebase_result.returncode == 0:
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
    spec: config.RepoSpec, worktree: Path
) -> Tuple[bool, list[str]]:
    """Compatibility alias for older patches/imports.

    TODO(remove after 2026-08-24): drop once out-of-repo patches have moved
    to `_rebase_base_into_worktree`.
    """
    return _rebase_base_into_worktree(spec, worktree)


def _rebase_state_exists(worktree: Path, state_dir: str) -> bool:
    """Resolve one git rebase-state path and report whether it exists."""
    git_path_result = _git_hardened(
        "rev-parse", "--git-path", state_dir, cwd=worktree,
    )
    if git_path_result.returncode != 0:
        return False
    path = (git_path_result.stdout or "").strip()
    if not path:
        return False
    state_path = Path(path)
    if not state_path.is_absolute():
        state_path = worktree / state_path
    return state_path.exists()


def _rebase_in_progress(worktree: Path) -> bool:
    """Return True when the worktree still has an unfinished rebase."""
    return any(
        _rebase_state_exists(worktree, state_dir)
        for state_dir in ("rebase-merge", "rebase-apply")
    )


def _issue_worktree_number(worktree: Path) -> Optional[int]:
    """Return an issue number only for a valid issue worktree directory."""
    if not worktree.is_dir() or not worktree.name.startswith("issue-"):
        return None
    try:
        return int(worktree.name[len("issue-"):])
    except ValueError:
        return None


def _sync_discovered_worktree(
    gh: GitHubClient,
    spec: config.RepoSpec,
    worktree: Path,
    issue_number: int,
    scheduler: Optional[IssueScheduler],
) -> None:
    """Sync one discovered worktree unless its handler is still active."""
    if scheduler is not None and scheduler.is_active(
        spec.slug, issue_number,
    ):
        log.debug(
            "repo=%s issue=#%d active in scheduler; skipping base "
            "sync until the worker completes", spec.slug, issue_number,
        )
        return
    try:
        _sync_worktree_with_base(gh, spec, worktree, issue_number)
    except Exception:
        log.exception(
            "repo=%s issue=#%d base sync failed; continuing",
            spec.slug, issue_number,
        )


def _refresh_base_and_worktrees(
    gh: GitHubClient,
    spec: config.RepoSpec,
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
      resolve the conflict. Issues already labeled
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

    for worktree in sorted(root.iterdir()):
        issue_number = _issue_worktree_number(worktree)
        if issue_number is not None:
            _sync_discovered_worktree(
                gh, spec, worktree, issue_number, scheduler,
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
# on a stale base and only the next in_review tick would catch it.
# `resolving_conflict` itself is excluded -- the handler runs this
# tick regardless and will do the rebase anyway. Other labels mean
# either no PR yet (pre-PR path applies instead) or terminal
# (done/rejected, nothing to refresh).
_PR_REFRESH_DETOUR_LABELS = frozenset(
    (
        WorkflowLabel.VALIDATING, WorkflowLabel.DOCUMENTING,
        WorkflowLabel.IN_REVIEW, WorkflowLabel.FIXING,
    ),
)

# Pinned-state keys and park-reason values this refresh path reads and writes.
_PARK_REASON = "park_reason"
_AWAITING_HUMAN = "awaiting_human"
_REVIEW_ROUND = "review_round"
_CONFLICT_ROUND = "conflict_round"
_PENDING_PUSH_SHA = "pending_auto_base_rebase_push_sha"
_REASON_AUTO_BASE_REBASE_FAILED = "auto_base_rebase_failed"
_REASON_AUTO_BASE_REBASE_PUSH_FAILED = "auto_base_rebase_push_failed"

# Longest error-text snippet embedded in an operator park comment.
_ERROR_SNIPPET_LEN = 120


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
    (
        _REASON_AUTO_BASE_REBASE_FAILED,
        "auto_base_rebase_dirty",
        _REASON_AUTO_BASE_REBASE_PUSH_FAILED,
    ),
)


@dataclass(frozen=True)
class _AutoRebaseContext:
    """Stable inputs for one refresh-time PR rebase attempt."""

    gh: GitHubClient
    spec: config.RepoSpec
    issue: Issue
    state: PinnedState
    worktree: Path
    pr_number: int
    behind: int
    label: Optional[WorkflowLabel]
    pending_pre_rebase_sha: Optional[str]


@dataclass(frozen=True)
class _AutoRebaseRecoveryContext:
    """Stable inputs for finalizing one interrupted auto-rebase."""

    gh: GitHubClient
    spec: config.RepoSpec
    issue: Issue
    state: PinnedState
    worktree: Path
    pr_number: int
    label: str
    pending_pre_rebase_sha: str
    behind: int = 0
    unparking_consumed_max: Optional[int] = None


@dataclass(frozen=True)
class _AutoRebaseRecoverySnapshot:
    """Local and remote branch state observed during crash recovery."""

    branch: str
    local_head: str
    remote_head: str = ""
    ahead: int = 0
    behind: int = 0


@dataclass(frozen=True)
class _AutoRebaseDecision:
    """Whether the coordinator should continue its normal rebase flow."""

    should_continue: bool
    consumed_comment_id: Optional[int] = None


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
    from orchestrator import workflow as _wf
    assert reason in _AUTO_REBASE_PARK_REASONS, (
        f"_park_auto_rebase_failure called with reason={reason!r}, "
        f"which is not in _AUTO_REBASE_PARK_REASONS"
    )
    _wf._park_awaiting_human(gh, issue, state, message, reason=reason)
    state.set(_PARK_REASON, reason)
    gh.write_pinned_state(issue, state)


def _reset_clear_and_park(
    context: _AutoRebaseContext | _AutoRebaseRecoveryContext,
    reset_sha: str,
    *,
    message: str,
    reason: str,
    clean: bool = False,
) -> None:
    """Restore the worktree to `reset_sha`, drop the recovery anchor, and park.

    The shared tail of every auto-rebase park path: a rebase / push /
    recovery step could not safely finalize, so HEAD is hard-reset back
    to a known SHA (the pre-rebase anchor = the last-known remote PR
    head) so the same-tick stage handler dispatch never reads a local
    HEAD the PR may not carry, the crash-recovery anchor is cleared (the
    reset put HEAD back at it, so a follow-up tick would only hit the
    "HEAD == anchor" no-op case), and the issue is parked awaiting human.
    `clean=True` also runs `git clean -fd` after the reset to discard the
    untracked leftovers a dirty rebase produced (recoverable via
    `git reflog`).

    A failed reset / clean is logged but does not abort the park: the
    `awaiting_human` flag is what short-circuits the same-tick handlers,
    and it still lands even if the worktree is left on an unexpected SHA
    for the operator to inspect.
    """
    reset = _git_hardened(
        "reset", "--hard", reset_sha, cwd=context.worktree,
    )
    if reset.returncode != 0:
        log.error(
            "issue=#%d auto-rebase recovery: reset --hard to %s failed: "
            "%s; the awaiting_human park still short-circuits same-tick "
            "handler dispatch but operator inspection of HEAD is needed",
            context.issue.number,
            reset_sha[:8],
            (reset.stderr or "").strip(),
        )
    if clean:
        cleaned = _git_hardened("clean", "-fd", cwd=context.worktree)
        if cleaned.returncode != 0:
            log.error(
                "issue=#%d auto-rebase recovery: `git clean -fd` after "
                "the reset failed: %s",
                context.issue.number, (cleaned.stderr or "").strip(),
            )
    context.state.set(_PENDING_PUSH_SHA, None)
    _park_auto_rebase_failure(
        context.gh,
        context.issue,
        context.state,
        message=message,
        reason=reason,
    )


def _prepare_recovered_rebase_state(
    context: _AutoRebaseRecoveryContext,
) -> None:
    """Clear the recovery anchor and commit any pending human retry."""
    if context.unparking_consumed_max is not None:
        context.state.set(
            "last_action_comment_id", context.unparking_consumed_max,
        )
        context.state.set(_AWAITING_HUMAN, False)
        context.state.set(_PARK_REASON, None)
    context.state.set(_PENDING_PUSH_SHA, None)
    context.state.set(_REVIEW_ROUND, 0)


def _post_recovered_rebase_notice(
    context: _AutoRebaseRecoveryContext, notice: str,
) -> None:
    """Post the recovery notice without blocking state finalization."""
    try:
        _post_pr_comment(
            context.gh, context.pr_number, context.state, notice,
        )
    except Exception:
        log.exception(
            "issue=#%s could not post auto-rebase recovery notice to "
            "PR #%s", context.issue.number, context.pr_number,
        )


def _emit_recovered_rebase_event(
    context: _AutoRebaseRecoveryContext,
    local_head: str,
    method: str,
) -> None:
    """Emit the stable audit shape for a recovered auto-rebase."""
    context.gh.emit_event(
        "base_rebased",
        issue_number=context.issue.number,
        stage=context.label,
        pr_number=context.pr_number,
        sha=local_head,
        method=method,
        review_round=0,
        retry_count=context.state.get("retry_count"),
    )


def _route_recovered_rebase(
    context: _AutoRebaseRecoveryContext,
    local_head: str,
    method: str,
) -> bool:
    """Persist recovery progress and route only a current head to validation."""
    if context.behind == 0:
        log.info(
            "issue=#%d auto-rebase recovery (%s): recovered head %s is "
            "current; routing %r -> validating",
            context.issue.number,
            method,
            local_head[:8],
            context.label,
        )
        context.gh.set_workflow_label(context.issue, "validating")
        context.gh.write_pinned_state(context.issue, context.state)
        return True
    context.gh.write_pinned_state(context.issue, context.state)
    log.info(
        "issue=#%d auto-rebase recovery (%s): recovered head %s is still "
        "%d commit(s) behind %s/%s; falling through to the normal rebase "
        "+ push flow",
        context.issue.number,
        method,
        local_head[:8],
        context.behind,
        context.spec.remote_name,
        context.spec.base_branch,
    )
    return False


def _finalize_recovered_rebase(
    context: _AutoRebaseRecoveryContext,
    *,
    local_head: str,
    method: str,
    notice: str,
) -> bool:
    """Finalize a recovered push and route it according to current base lag."""
    _prepare_recovered_rebase_state(context)
    _post_recovered_rebase_notice(context, notice)
    _emit_recovered_rebase_event(context, local_head, method)
    return _route_recovered_rebase(context, local_head, method)


def _abort_recovery_unverified(
    context: _AutoRebaseRecoveryContext, detail: str,
) -> bool:
    """Restore the recovery anchor when the remote state cannot be verified."""
    pre_rebase_short = context.pending_pre_rebase_sha[:8]
    _reset_clear_and_park(
        context,
        context.pending_pre_rebase_sha,
        message=(
            f"{config.HITL_MENTIONS} crash recovery for PR "
            f"#{context.pr_number} could not safely finalize: {detail} "
            f"Local HEAD has been reset to the pre-rebase SHA "
            f"`{pre_rebase_short}` so the worktree "
            "matches the (last-known) remote PR head -- the "
            "issue is parked so the same-tick stage handlers do "
            "NOT run against a SHA the PR may not carry. Reply "
            "on this issue with anything once the underlying "
            "problem is fixed and the orchestrator will re-"
            "attempt the auto rebase on the next polling tick."
        ),
        reason=_REASON_AUTO_BASE_REBASE_PUSH_FAILED,
    )
    return True


def _clear_ineligible_recovery(
    context: _AutoRebaseRecoveryContext,
) -> bool:
    """Clear an interrupted-rebase anchor after an operator relabel."""
    context.state.set(_PENDING_PUSH_SHA, None)
    context.gh.write_pinned_state(context.issue, context.state)
    log.info(
        "issue=#%d auto-rebase recovery: label %r is no longer in "
        "the refresh-driven set; clearing pending flag",
        context.issue.number,
        context.label,
    )
    return True


def _fetch_recovery_snapshot(
    context: _AutoRebaseRecoveryContext,
) -> Optional[_AutoRebaseRecoverySnapshot]:
    """Fetch the PR branch and capture the local recovery head."""
    spec = context.spec
    branch = _resolve_branch_name(
        context.state, spec, context.issue.number,
    )
    fetch_result = _authed_fetch(
        spec,
        f"+refs/heads/{branch}:refs/remotes/"
        f"{spec.remote_name}/{branch}",
        cwd=context.worktree,
    )
    if fetch_result.returncode != 0:
        fetch_error = (fetch_result.stderr or "").strip()
        log.warning(
            "issue=#%d auto-rebase recovery fetch of %s/%s failed: %s; "
            "aborting recovery and parking awaiting human",
            context.issue.number,
            spec.remote_name,
            branch,
            fetch_error,
        )
        error_snippet = fetch_error[:_ERROR_SNIPPET_LEN]
        _abort_recovery_unverified(
            context,
            f"the fetch of `{spec.remote_name}/{branch}` "
            "needed to verify the recovered SHA against the remote PR "
            f"head failed (`{error_snippet}`).",
        )
        return None
    return _AutoRebaseRecoverySnapshot(
        branch=branch,
        local_head=_head_sha(context.worktree) or "",
    )


def _clear_unchanged_recovery(
    context: _AutoRebaseRecoveryContext,
) -> bool:
    """Clear an anchor when HEAD never moved beyond the pre-rebase SHA."""
    context.state.set(_PENDING_PUSH_SHA, None)
    context.gh.write_pinned_state(context.issue, context.state)
    log.info(
        "issue=#%d auto-rebase recovery: local HEAD matches pre-"
        "rebase SHA `%s`; clearing flag and falling through to "
        "the normal rebase flow",
        context.issue.number,
        context.pending_pre_rebase_sha[:8],
    )
    return False


def _read_remote_recovery_head(
    context: _AutoRebaseRecoveryContext,
    branch: str,
) -> Optional[str]:
    """Read the freshly fetched remote PR head or park fail-closed."""
    spec = context.spec
    remote_ref = f"refs/remotes/{spec.remote_name}/{branch}"
    remote_head_result = _git_hardened(
        "rev-parse", remote_ref, cwd=context.worktree,
    )
    if remote_head_result.returncode != 0:
        remote_error = (remote_head_result.stderr or "").strip()
        log.warning(
            "issue=#%d auto-rebase recovery: rev-parse of %s failed "
            "after fetch: %s; aborting recovery and parking awaiting human",
            context.issue.number,
            remote_ref,
            remote_error,
        )
        remote_error = remote_error[:_ERROR_SNIPPET_LEN]
        _abort_recovery_unverified(
            context,
            f"`git rev-parse {remote_ref}` failed after the fetch "
            f"(`{remote_error}`), so the remote PR head SHA "
            "needed for the equality check could not be read.",
        )
        return None
    remote_head = (remote_head_result.stdout or "").strip()
    if remote_head:
        return remote_head
    log.warning(
        "issue=#%d auto-rebase recovery: rev-parse of %s returned "
        "no SHA; aborting recovery and parking awaiting human",
        context.issue.number,
        remote_ref,
    )
    _abort_recovery_unverified(
        context,
        f"`git rev-parse {remote_ref}` returned no SHA after the "
        "fetch, so the remote PR head SHA needed for the equality "
        "check could not be read.",
    )
    return None


def _complete_recovery_snapshot(
    context: _AutoRebaseRecoveryContext,
    snapshot: _AutoRebaseRecoverySnapshot,
) -> Optional[_AutoRebaseRecoverySnapshot]:
    """Add the verified remote head and divergence counts to a snapshot."""
    remote_head = _read_remote_recovery_head(context, snapshot.branch)
    if remote_head is None:
        return None
    if snapshot.local_head == remote_head:
        return _AutoRebaseRecoverySnapshot(
            branch=snapshot.branch,
            local_head=snapshot.local_head,
            remote_head=remote_head,
        )
    ahead, behind = _branch_ahead_behind(
        context.spec, context.worktree, snapshot.branch,
    )
    return _AutoRebaseRecoverySnapshot(
        branch=snapshot.branch,
        local_head=snapshot.local_head,
        remote_head=remote_head,
        ahead=ahead,
        behind=behind,
    )


def _already_published_recovery_notice(
    context: _AutoRebaseRecoveryContext,
    local_head: str,
) -> str:
    """Format the notice for a recovery push that landed before restart."""
    short_head = local_head[:8]
    notice = (
        f":mag: Recovered an interrupted auto-rebase for PR "
        f"#{context.pr_number}; the new head `{short_head}` was "
        "already published before the orchestrator restart."
    )
    if context.behind == 0:
        return (
            notice
            + f" Routing `{context.label}` -> `validating` so the "
            "reviewer re-runs against the rewritten branch."
        )
    return (
        notice
        + f" Base advanced again by {context.behind} commit(s)"
        " since the interrupted rebase; rebasing once "
        "more before routing to `validating`."
    )


def _pushed_recovery_notice(
    context: _AutoRebaseRecoveryContext,
    local_head: str,
) -> str:
    """Format the notice for a recovery push reissued this tick."""
    short_head = local_head[:8]
    notice = (
        f":mag: Recovered an interrupted auto-rebase for PR "
        f"#{context.pr_number}; pushed the recovered head "
        f"`{short_head}`."
    )
    if context.behind == 0:
        return f"{notice} Routing `{context.label}` -> `validating`."
    return (
        notice
        + f" Base advanced again by {context.behind} commit(s) "
        "since the interrupted rebase; rebasing once more "
        "before routing to `validating`."
    )


def _finalize_already_published_recovery(
    context: _AutoRebaseRecoveryContext,
    snapshot: _AutoRebaseRecoverySnapshot,
) -> bool:
    """Finalize state after confirming that the interrupted push landed."""
    return _finalize_recovered_rebase(
        context,
        local_head=snapshot.local_head,
        method="crash_recovery_relabel_only",
        notice=_already_published_recovery_notice(
            context, snapshot.local_head,
        ),
    )


def _reject_unknown_recovery_comparison(
    context: _AutoRebaseRecoveryContext,
    snapshot: _AutoRebaseRecoverySnapshot,
) -> bool:
    """Park when unequal heads cannot be classified as ahead or behind."""
    log.warning(
        "issue=#%d auto-rebase recovery: local HEAD (`%s`) differs "
        "from remote PR head (`%s`) but `_branch_ahead_behind` "
        "returned `(0, 0)`; aborting recovery and parking awaiting "
        "human",
        context.issue.number,
        snapshot.local_head[:8],
        snapshot.remote_head[:8],
    )
    local_short = snapshot.local_head[:8]
    remote_short = snapshot.remote_head[:8]
    return _abort_recovery_unverified(
        context,
        f"local HEAD `{local_short}` differs from remote "
        f"PR head `{remote_short}` but "
        "`_branch_ahead_behind` returned `(0, 0)`, which means the "
        "remote-tracking ref we just fetched is unexpectedly missing "
        "-- the path the recovery would take next cannot be determined "
        "safely.",
    )


def _park_diverged_recovery(
    context: _AutoRebaseRecoveryContext,
    snapshot: _AutoRebaseRecoverySnapshot,
) -> bool:
    """Restore the anchor instead of overwriting an out-of-band PR update."""
    spec = context.spec
    local_short = snapshot.local_head[:8]
    pre_rebase_short = context.pending_pre_rebase_sha[:8]
    _reset_clear_and_park(
        context,
        context.pending_pre_rebase_sha,
        message=(
            f"{config.HITL_MENTIONS} crash recovery for PR "
            f"#{context.pr_number}: local worktree "
            f"(`{local_short}`) is {snapshot.ahead} ahead "
            f"and {snapshot.behind} behind remote "
            f"`{spec.remote_name}/{snapshot.branch}` -- the "
            "remote PR branch was updated out-of-band during the "
            "interrupted auto rebase. HEAD has been reset to the pre-"
            f"rebase SHA `{pre_rebase_short}`. "
            "Investigate the remote PR head and reply on this issue "
            "with anything once the divergence is reconciled."
        ),
        reason=_REASON_AUTO_BASE_REBASE_PUSH_FAILED,
    )
    return True


def _park_dirty_recovery(
    context: _AutoRebaseRecoveryContext,
    snapshot: _AutoRebaseRecoverySnapshot,
    dirty_files: list[str],
) -> bool:
    """Reset and clean a recovered rebase that carries worktree changes."""
    local_short = snapshot.local_head[:8]
    pre_rebase_short = context.pending_pre_rebase_sha[:8]
    _reset_clear_and_park(
        context,
        context.pending_pre_rebase_sha,
        message=(
            f"{config.HITL_MENTIONS} crash recovery for PR "
            f"#{context.pr_number}: the rebased worktree (recovered "
            f"from a prior tick, HEAD `{local_short}`) "
            f"carries {len(dirty_files)} uncommitted change(s). HEAD "
            "has been reset to the pre-rebase SHA "
            f"`{pre_rebase_short}` and untracked "
            "files cleaned (use `git reflog` if you need the "
            "discarded edits). Investigate, then reply on this issue "
            "with anything to retry."
        ),
        reason="auto_base_rebase_dirty",
        clean=True,
    )
    return True


def _park_failed_recovery_push(
    context: _AutoRebaseRecoveryContext,
    snapshot: _AutoRebaseRecoverySnapshot,
) -> bool:
    """Restore the anchor after a recovered force-push fails."""
    local_short = snapshot.local_head[:8]
    pre_rebase_short = context.pending_pre_rebase_sha[:8]
    _reset_clear_and_park(
        context,
        context.pending_pre_rebase_sha,
        message=(
            f"{config.HITL_MENTIONS} crash recovery for PR "
            f"#{context.pr_number}: `--force-with-lease` push of the "
            f"recovered rebase (`{local_short}`, lease "
            f"against `{pre_rebase_short}`) failed. "
            "HEAD has been reset to the pre-rebase SHA. Most likely "
            "the remote PR branch was updated out-of-band; investigate "
            "and reply on this issue with anything to retry."
        ),
        reason=_REASON_AUTO_BASE_REBASE_PUSH_FAILED,
    )
    return True


def _retry_recovery_push(
    context: _AutoRebaseRecoveryContext,
    snapshot: _AutoRebaseRecoverySnapshot,
) -> bool:
    """Publish a verified ahead-only recovery head and finalize its state."""
    dirty_files = _worktree_dirty_files(context.worktree)
    if dirty_files:
        return _park_dirty_recovery(context, snapshot, dirty_files)
    if not _push_branch(
        context.spec,
        context.worktree,
        snapshot.branch,
        force_with_lease=context.pending_pre_rebase_sha,
    ):
        return _park_failed_recovery_push(context, snapshot)
    return _finalize_recovered_rebase(
        context,
        local_head=snapshot.local_head,
        method="crash_recovery_pushed",
        notice=_pushed_recovery_notice(context, snapshot.local_head),
    )


def _recover_pending_auto_base_rebase_context(
    context: _AutoRebaseRecoveryContext,
) -> bool:
    """Route an interrupted auto-rebase from verified local/remote state."""
    if context.label not in _PR_REFRESH_DETOUR_LABELS:
        return _clear_ineligible_recovery(context)

    snapshot = _fetch_recovery_snapshot(context)
    if snapshot is None:
        return True
    if (
        snapshot.local_head
        and snapshot.local_head == context.pending_pre_rebase_sha
    ):
        return _clear_unchanged_recovery(context)

    return _route_recovery_snapshot(context, snapshot)


def _route_recovery_snapshot(
    context: _AutoRebaseRecoveryContext, snapshot: _AutoRebaseRecoverySnapshot,
) -> bool:
    """Route a changed-head recovery from its completed local/remote compare."""
    snapshot = _complete_recovery_snapshot(context, snapshot)
    if snapshot is None:
        return True
    if snapshot.local_head and snapshot.local_head == snapshot.remote_head:
        return _finalize_already_published_recovery(context, snapshot)
    if snapshot.ahead == 0 and snapshot.behind == 0:
        return _reject_unknown_recovery_comparison(context, snapshot)
    if snapshot.behind > 0:
        return _park_diverged_recovery(context, snapshot)
    return _retry_recovery_push(context, snapshot)


def _recover_pending_auto_base_rebase(
    gh: GitHubClient,
    spec: config.RepoSpec,
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

    The pinned pre-rebase SHA distinguishes an unchanged worktree, an
    already-published rewrite, an ahead-only rewrite that still needs a
    push, and a branch that diverged through an out-of-band update. Returns
    False only when HEAD still equals the anchor and the normal rebase flow
    should continue on the same tick.
    """
    context = _AutoRebaseRecoveryContext(
        gh=gh,
        spec=spec,
        issue=issue,
        state=state,
        worktree=worktree,
        pr_number=pr_number,
        label=label,
        pending_pre_rebase_sha=pending_pre_rebase_sha,
        behind=behind,
        unparking_consumed_max=unparking_consumed_max,
    )
    return _recover_pending_auto_base_rebase_context(context)


def _base_sync_issue(
    gh: GitHubClient, issue_number: int,
) -> Optional[Issue]:
    """Return the issue for a worktree, or None when it is not retrievable."""
    try:
        return gh.get_issue(issue_number)
    except Exception:
        log.debug(
            "issue=#%d not retrievable; skipping base sync", issue_number,
        )
        return None


def _issue_skips_base_sync(issue: Issue, issue_number: int) -> bool:
    """Apply dispatcher hard-skips and the question-stage read-only gate."""
    skip_label = hard_skip_control_label(issue)
    if skip_label is not None:
        log.debug(
            "issue=#%d has %r; skipping base sync",
            issue_number,
            skip_label,
        )
        return True
    if not issue_has_label(issue, "question"):
        return False
    log.debug(
        "issue=#%d has 'question' label; skipping base sync "
        "(read-only stage)",
        issue_number,
    )
    return True


def _worktree_behind_base(
    spec: config.RepoSpec, worktree: Path, issue_number: int,
) -> Optional[int]:
    """Return the base lag, or None when the comparison cannot be read."""
    base_ref = f"{spec.remote_name}/{spec.base_branch}"
    behind_result = _git(
        "rev-list", "--count", f"HEAD..{base_ref}", cwd=worktree,
    )
    if behind_result.returncode != 0:
        log.debug(
            "issue=#%d skipping base sync: rev-list failed: %s",
            issue_number,
            (behind_result.stderr or "").strip(),
        )
        return None
    try:
        return int((behind_result.stdout or "0").strip() or "0")
    except ValueError:
        return None


def _sync_pre_pr_worktree(
    spec: config.RepoSpec,
    worktree: Path,
    issue_number: int,
    behind: int,
) -> None:
    """Rebase one clean pre-PR worktree and restore it on failure."""
    base_ref = f"{spec.remote_name}/{spec.base_branch}"
    succeeded, conflicted_files = _rebase_base_into_worktree(spec, worktree)
    if succeeded:
        log.info(
            "issue=#%d rebased worktree onto %s (was %d commit(s) behind)",
            issue_number,
            base_ref,
            behind,
        )
        return

    abort_result = _git_hardened("rebase", "--abort", cwd=worktree)
    if abort_result.returncode != 0:
        log.warning(
            "issue=#%d base rebase failed and abort failed: %s",
            issue_number,
            (abort_result.stderr or "").strip(),
        )
    if conflicted_files:
        log.info(
            "issue=#%d base rebase has %d conflict(s); aborted -- "
            "resolving_conflict will handle it once a PR exists",
            issue_number,
            len(conflicted_files),
        )
        return
    log.warning(
        "issue=#%d base rebase failed without conflicted files; aborted",
        issue_number,
    )


def _sync_worktree_with_base(
    gh: GitHubClient, spec: config.RepoSpec, worktree: Path, issue_number: int,
) -> None:
    """Bring one per-issue worktree up to date with the configured base.

    Pre-PR worktrees are rebased locally when clean. PR worktrees always
    reach the PR-aware coordinator so a pinned crash-recovery anchor is
    honored even when local HEAD already contains the latest base.
    """
    issue = _base_sync_issue(gh, issue_number)
    if issue is None or _issue_skips_base_sync(issue, issue_number):
        return

    state = gh.read_pinned_state(issue)
    pr_number = state.get("pr_number")
    if pr_number is None and _worktree_dirty_files(worktree):
        log.debug(
            "issue=#%d skipping base sync: worktree has uncommitted changes",
            issue_number,
        )
        return

    behind = _worktree_behind_base(spec, worktree, issue_number)
    if behind is None:
        return
    if pr_number is not None:
        _sync_pr_worktree_to_base(
            gh, spec, issue, state, worktree, int(pr_number), behind,
        )
        return
    if behind:
        _sync_pre_pr_worktree(spec, worktree, issue_number, behind)


def _auto_rebase_label_is_eligible(context: _AutoRebaseContext) -> bool:
    """Clear stale recovery state and reject labels refresh does not drive."""
    if context.label in _PR_REFRESH_DETOUR_LABELS:
        return True
    if context.pending_pre_rebase_sha:
        _recover_pending_auto_base_rebase(
            context.gh,
            context.spec,
            context.issue,
            context.state,
            context.worktree,
            pr_number=context.pr_number,
            label=context.label,
            pending_pre_rebase_sha=str(context.pending_pre_rebase_sha),
        )
    log.debug(
        "issue=#%d behind %s/%s by %d but label=%r; not auto-rebasing",
        context.issue.number,
        context.spec.remote_name,
        context.spec.base_branch,
        context.behind,
        context.label,
    )
    return False


def _auto_rebase_retry_decision(
    context: _AutoRebaseContext,
) -> _AutoRebaseDecision:
    """Keep stage-owned parks intact and recognize a trusted retry reply."""
    if not context.state.get(_AWAITING_HUMAN):
        return _AutoRebaseDecision(should_continue=True)

    park_reason = context.state.get(_PARK_REASON)
    if park_reason not in _AUTO_REBASE_PARK_REASONS:
        log.debug(
            "issue=#%d behind %s/%s by %d but awaiting_human=True "
            "with park_reason=%r; leaving park intact rather than "
            "auto-rebasing",
            context.issue.number,
            context.spec.remote_name,
            context.spec.base_branch,
            context.behind,
            park_reason,
        )
        return _AutoRebaseDecision(should_continue=False)

    last_action_id = context.state.get("last_action_comment_id")
    new_comments = filter_trusted(
        context.gh.comments_after(context.issue, last_action_id)
    )
    if not new_comments:
        log.debug(
            "issue=#%d behind %s/%s by %d, parked on %r with no new "
            "human comment; staying parked",
            context.issue.number,
            context.spec.remote_name,
            context.spec.base_branch,
            context.behind,
            park_reason,
        )
        return _AutoRebaseDecision(should_continue=False)

    consumed_comment_id = max(comment.id for comment in new_comments)
    log.info(
        "issue=#%d parked on %r had a new human comment; will clear "
        "the park if a retry is actually attempted this tick (gates "
        "that early-return preserve the park on disk so the "
        "operator's reply is not silently consumed)",
        context.issue.number,
        park_reason,
    )
    return _AutoRebaseDecision(
        should_continue=True,
        consumed_comment_id=consumed_comment_id,
    )


def _open_auto_rebase_pr(
    context: _AutoRebaseContext,
) -> Optional[PullRequest]:
    """Return the open PR or leave terminal and unreadable PRs untouched."""
    try:
        pr = context.gh.get_pr(context.pr_number)
    except Exception:
        log.debug(
            "issue=#%d could not fetch PR #%d for refresh rebase; "
            "leaving label alone, handler will retry next tick",
            context.issue.number,
            context.pr_number,
        )
        return None

    pr_status = context.gh.pr_state(pr)
    if pr_status == "open":
        return pr
    if context.pending_pre_rebase_sha:
        context.state.set(_PENDING_PUSH_SHA, None)
        context.gh.write_pinned_state(context.issue, context.state)
        log.info(
            "issue=#%d PR #%d is %s and a recovery anchor was "
            "pinned; clearing the stale flag",
            context.issue.number,
            context.pr_number,
            pr_status,
        )
    log.debug(
        "issue=#%d PR #%d is %s; not auto-rebasing (handler will finalize)",
        context.issue.number,
        context.pr_number,
        pr_status,
    )
    return None


def _auto_rebase_recovery_decision(
    context: _AutoRebaseContext,
    consumed_comment_id: Optional[int],
) -> _AutoRebaseDecision:
    """Run pending crash recovery and retain only an uncommitted retry."""
    if not context.pending_pre_rebase_sha:
        return _AutoRebaseDecision(True, consumed_comment_id)
    if _recover_pending_auto_base_rebase(
        context.gh,
        context.spec,
        context.issue,
        context.state,
        context.worktree,
        pr_number=context.pr_number,
        label=context.label,
        pending_pre_rebase_sha=str(context.pending_pre_rebase_sha),
        behind=context.behind,
        unparking_consumed_max=consumed_comment_id,
    ):
        return _AutoRebaseDecision(should_continue=False)
    if not context.state.get(_AWAITING_HUMAN):
        consumed_comment_id = None
    return _AutoRebaseDecision(True, consumed_comment_id)


def _normal_auto_rebase_can_start(context: _AutoRebaseContext) -> bool:
    """Apply the clean-tree probe before deciding whether base is behind."""
    if _worktree_dirty_files(context.worktree):
        log.debug(
            "issue=#%d skipping base sync: worktree has uncommitted changes",
            context.issue.number,
        )
        return False
    return context.behind != 0


def _park_unreadable_pre_rebase_head(context: _AutoRebaseContext) -> None:
    """Fail closed when the lease and recovery anchor cannot be read."""
    log.error(
        "issue=#%d cannot read local HEAD before auto base rebase; "
        "parking awaiting human (no rebase attempted)",
        context.issue.number,
    )
    spec = context.spec
    _park_auto_rebase_failure(
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
    consumed_comment_id: Optional[int],
) -> None:
    """Persist the recovery anchor and any retry unpark before git runs."""
    if consumed_comment_id is not None:
        context.state.set("last_action_comment_id", consumed_comment_id)
        context.state.set(_AWAITING_HUMAN, False)
        context.state.set(_PARK_REASON, None)
    context.state.set(_PENDING_PUSH_SHA, before_sha)
    context.gh.write_pinned_state(context.issue, context.state)


def _handle_failed_auto_rebase(
    context: _AutoRebaseContext,
    pr: PullRequest,
    conflicted_files: list[str],
) -> None:
    """Abort a failed rebase, then route conflicts or park other failures."""
    abort = _git_hardened("rebase", "--abort", cwd=context.worktree)
    if abort.returncode != 0:
        log.warning(
            "issue=#%d base rebase failed and abort failed: %s",
            context.issue.number,
            (abort.stderr or "").strip(),
        )
    context.state.set(_PENDING_PUSH_SHA, None)
    if conflicted_files:
        _route_pr_worktree_to_resolving_conflict(
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
    _park_auto_rebase_failure(
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


def _start_auto_rebase(
    context: _AutoRebaseContext,
    pr: PullRequest,
    consumed_comment_id: Optional[int],
) -> Optional[str]:
    """Anchor and execute the rebase, returning the known pre-rebase SHA."""
    before_sha = _head_sha(context.worktree) or ""
    if not before_sha:
        _park_unreadable_pre_rebase_head(context)
        return None
    _record_auto_rebase_attempt(context, before_sha, consumed_comment_id)
    succeeded, conflicted_files = _rebase_base_into_worktree(
        context.spec, context.worktree,
    )
    if not succeeded:
        _handle_failed_auto_rebase(context, pr, conflicted_files)
        return None
    return before_sha


def _park_unreadable_post_rebase_head(
    context: _AutoRebaseContext,
    before_sha: str,
) -> None:
    """Restore the known PR head when the rebased HEAD cannot be read."""
    log.error(
        "issue=#%d cannot read local HEAD after auto base rebase; "
        "resetting to pre-rebase SHA and parking awaiting human",
        context.issue.number,
    )
    spec = context.spec
    before_short = before_sha[:8]
    _reset_clear_and_park(
        context,
        before_sha,
        message=(
            f"{config.HITL_MENTIONS} PR #{context.pr_number} is "
            f"{context.behind} commit(s) behind "
            f"`{spec.remote_name}/{spec.base_branch}`. "
            "The auto rebase ran but the orchestrator could not "
            "read local `HEAD` afterwards. HEAD has been reset to "
            f"the pre-rebase SHA `{before_short}` so the worktree "
            "still matches the remote PR head. Inspect the "
            "worktree's git state and reply on this issue with "
            "anything to retry."
        ),
        reason=_REASON_AUTO_BASE_REBASE_FAILED,
    )


def _finish_noop_auto_rebase(context: _AutoRebaseContext) -> None:
    """Clear the recovery anchor when the rebase leaves HEAD unchanged."""
    log.info(
        "issue=#%d base rebase was a no-op despite %d commit(s) "
        "behind %s/%s; leaving label alone",
        context.issue.number,
        context.behind,
        context.spec.remote_name,
        context.spec.base_branch,
    )
    context.state.set(_PENDING_PUSH_SHA, None)
    context.gh.write_pinned_state(context.issue, context.state)


def _park_dirty_auto_rebase(
    context: _AutoRebaseContext,
    before_sha: str,
    dirty_files: list[str],
) -> None:
    """Reset and park rather than publish a rebase with worktree edits."""
    log.warning(
        "issue=#%d worktree has %d uncommitted change(s) after "
        "auto base rebase; resetting HEAD and parking awaiting human",
        context.issue.number,
        len(dirty_files),
    )
    spec = context.spec
    _reset_clear_and_park(
        context,
        before_sha,
        message=(
            f"{config.HITL_MENTIONS} PR #{context.pr_number} is "
            f"{context.behind} commit(s) behind "
            f"`{spec.remote_name}/{spec.base_branch}` "
            "and the auto rebase landed cleanly but left "
            f"{len(dirty_files)} uncommitted change(s) on the worktree. "
            "Local HEAD has been reset to the pre-rebase SHA and "
            "untracked files cleaned (use `git reflog` if you need "
            "the discarded edits). Investigate the smudge filter / "
            "hook / external race that produced the dirty tree, "
            "then reply on this issue with anything to retry."
        ),
        reason="auto_base_rebase_dirty",
        clean=True,
    )


def _park_failed_auto_rebase_push(
    context: _AutoRebaseContext,
    before_sha: str,
    branch: str,
) -> None:
    """Reset and park after a force-with-lease rejection or push failure."""
    spec = context.spec
    before_short = (before_sha or "")[:8]
    _reset_clear_and_park(
        context,
        before_sha,
        message=(
            f"{config.HITL_MENTIONS} PR #{context.pr_number} is "
            f"{context.behind} commit(s) behind "
            f"`{spec.remote_name}/{spec.base_branch}`; "
            "the orchestrator rebased the worktree cleanly but pushing "
            "the rewritten branch (`--force-with-lease` against "
            f"`{before_short}`) failed. Local HEAD has "
            "been reset to the pre-rebase SHA so the worktree still "
            "matches the remote PR head. Most likely the PR branch "
            "was updated out-of-band; investigate the remote "
            f"`{branch}` and reply on this issue with anything once "
            "the branch is ready for the orchestrator to re-attempt "
            "the auto-rebase on the next polling tick."
        ),
        reason=_REASON_AUTO_BASE_REBASE_PUSH_FAILED,
    )
    log.warning(
        "issue=#%d auto base rebase pushed nothing (lease rejection "
        "or push failure); local HEAD reset and issue parked awaiting "
        "human so the in_review / fixing / validating / documenting "
        "handlers do not process the issue on a behind-base PR head "
        "this tick",
        context.issue.number,
    )


def _post_auto_rebase_notice(
    context: _AutoRebaseContext,
    after_sha: str,
) -> None:
    """Post the best-effort PR notice for a published clean rebase."""
    spec = context.spec
    after_short = after_sha[:8]
    try:
        _post_pr_comment(
            context.gh,
            context.pr_number,
            context.state,
            f":mag: PR was {context.behind} commit(s) behind "
            f"`{spec.remote_name}/{spec.base_branch}`; "
            "orchestrator auto-rebased the branch and re-pushed it. "
            f"Routing `{context.label}` -> `validating` so the reviewer "
            f"re-runs against the new head (`{after_short}`).",
        )
    except Exception:
        log.exception(
            "issue=#%s could not post auto-rebase notice to PR #%s",
            context.issue.number,
            context.pr_number,
        )


def _emit_auto_rebase_event(
    context: _AutoRebaseContext,
    after_sha: str,
) -> None:
    """Emit the stable audit shape for a published clean rebase."""
    context.gh.emit_event(
        "base_rebased",
        issue_number=context.issue.number,
        stage=context.label,
        pr_number=context.pr_number,
        sha=after_sha,
        method="auto_clean_rebase",
        review_round=int(context.state.get(_REVIEW_ROUND) or 0),
        retry_count=context.state.get("retry_count"),
    )


def _finalize_auto_rebase(
    context: _AutoRebaseContext,
    branch: str,
    after_sha: str,
) -> None:
    """Publish the notice, audit event, validating route, and pinned state."""
    _post_auto_rebase_notice(context, after_sha)
    context.state.set(_PENDING_PUSH_SHA, None)
    context.state.set(_REVIEW_ROUND, 0)
    log.info(
        "issue=#%d auto base rebase pushed %s/%s -> %s; routing %r -> "
        "validating",
        context.issue.number,
        context.spec.remote_name,
        branch,
        after_sha[:8],
        context.label,
    )
    _emit_auto_rebase_event(context, after_sha)
    context.gh.set_workflow_label(context.issue, "validating")
    context.gh.write_pinned_state(context.issue, context.state)


def _publish_auto_rebase(
    context: _AutoRebaseContext,
    before_sha: str,
) -> None:
    """Validate and force-publish a successfully rebased PR worktree."""
    after_sha = _head_sha(context.worktree)
    if not after_sha:
        _park_unreadable_post_rebase_head(context, before_sha)
        return
    if after_sha == before_sha:
        _finish_noop_auto_rebase(context)
        return

    dirty_files = _worktree_dirty_files(context.worktree)
    if dirty_files:
        _park_dirty_auto_rebase(context, before_sha, dirty_files)
        return

    branch = _resolve_branch_name(
        context.state, context.spec, context.issue.number,
    )
    if not _push_branch(
        context.spec,
        context.worktree,
        branch,
        force_with_lease=before_sha or None,
    ):
        _park_failed_auto_rebase_push(context, before_sha, branch)
        return
    _finalize_auto_rebase(context, branch, after_sha)


def _sync_pr_worktree_to_base(
    gh: GitHubClient,
    spec: config.RepoSpec,
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
    context = _AutoRebaseContext(
        gh=gh,
        spec=spec,
        issue=issue,
        state=state,
        worktree=worktree,
        pr_number=pr_number,
        behind=behind,
        label=gh.workflow_label(issue),
        pending_pre_rebase_sha=state.get(
            _PENDING_PUSH_SHA
        ),
    )
    if not _auto_rebase_label_is_eligible(context):
        return

    retry = _auto_rebase_retry_decision(context)
    if not retry.should_continue:
        return
    pr = _open_auto_rebase_pr(context)
    if pr is None:
        return

    _publish_auto_rebase_from_pr(context, pr, retry.consumed_comment_id)


def _publish_auto_rebase_from_pr(
    context: _AutoRebaseContext, pr: PullRequest, consumed_comment_id: Optional[int],
) -> None:
    """Complete the recovery / rebase / publish phase for an opened PR."""
    recovery = _auto_rebase_recovery_decision(context, consumed_comment_id)
    if not recovery.should_continue:
        return
    if not _normal_auto_rebase_can_start(context):
        return

    before_sha = _start_auto_rebase(
        context, pr, recovery.consumed_comment_id,
    )
    if before_sha is None:
        return

    _publish_auto_rebase(context, before_sha)


def _route_pr_worktree_to_resolving_conflict(
    gh: GitHubClient,
    spec: config.RepoSpec,
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
    if state.get(_CONFLICT_ROUND) is None:
        state.set(_CONFLICT_ROUND, 0)

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
        _CONFLICT_ROUND,
        issue_number=issue.number,
        stage=label,
        pr_number=pr_number,
        sha=pr_head_sha or None,
        action="entered",
        conflict_round=int(state.get(_CONFLICT_ROUND) or 0),
        review_round=int(state.get(_REVIEW_ROUND) or 0),
        retry_count=state.get("retry_count"),
    )
    gh.set_workflow_label(issue, WorkflowLabel.RESOLVING_CONFLICT)
    gh.write_pinned_state(issue, state)
