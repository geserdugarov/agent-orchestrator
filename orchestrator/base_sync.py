# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Per-tick base refresh, rebase routing, and resolving-conflict detour.

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
  the per-tick refresh is willing to detour into `resolving_conflict`.
* `_sync_worktree_with_base` -- per-worktree dispatch: pre-PR rebase or
  PR-having `resolving_conflict` detour, with skip rules for dirty
  trees, `backlog`, `hold_base_sync`, and the `question` label.
* `_route_pr_worktree_to_resolving_conflict` -- relabel a behind-base
  PR-having issue to `resolving_conflict` so the existing handler runs
  rebase + push + relabel back to `validating`.

Imports the hardened git subprocess layer from `git_plumbing.py`, the
worktree-layout helpers from `worktree_lifecycle.py`, the worktree-
state probe (`_worktree_dirty_files`) from `verify.py`, and the
PR-comment helper from `workflow_messages.py`. `worktrees.py`
re-exports every name below under its original name so existing imports
(`from orchestrator.worktrees import _refresh_base_and_worktrees`) and
`patch.object(worktrees, "_foo", ...)` test patches that still target
the worktrees module keep resolving the symbol -- but the actual call
graph lives here, so test patches that need to INTERCEPT a call from
inside `_refresh_base_and_worktrees` / `_sync_worktree_with_base`
should target this module (`base_sync`) directly.

Each helper preserves the existing security hardening and workflow
semantics; downstream behavior is unchanged by this extraction. Helpers
remain prefixed with `_` because they are module-internal contracts --
the public surface (the dispatcher entry points and the stage handlers
they route to) still lives in `workflow.py` and `orchestrator/stages/`.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional, Tuple

from github.Issue import Issue

from .config import RepoSpec
from .git_plumbing import _authed_target_fetch, _git, _git_hardened
from .github import (
    BACKLOG_LABEL,
    BASE_SYNC_HOLD_LABEL,
    GitHubClient,
    PinnedState,
    issue_has_label,
)
from .scheduler import IssueScheduler
from .verify import _worktree_dirty_files
from .workflow_messages import _post_pr_comment
from .worktree_lifecycle import _repo_worktrees_root

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
      (the lease compares against the un-rebased remote tip). So instead
      we route the issue to `resolving_conflict`: the existing handler
      does the rebase, pushes, and flips back to `validating` (the same
      target as the base-up-to-date no-op exit) so the reviewer re-runs
      against the rewritten branch directly; the single docs pass is
      deferred to the post-approval handoff to `documenting` in
      `_handle_validating`. Applying the `hold_base_sync` label to
      an issue pauses both the pre-PR local rebase and the PR detour
      until the label is removed.
      Issues already labeled `resolving_conflict` are left alone (the
      handler runs this tick anyway); other labels are skipped (no PR
      worktree to refresh in those states).

    Rebase keeps the PR history linear after sibling PRs land. The handler
    resets `review_round` on every pushed rebase, so the reviewer re-runs
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


# Workflow labels the pre-tick refresh is willing to detour into
# `resolving_conflict` when the PR worktree is behind base. Validating,
# documenting, in_review, and fixing are the PR-stage labels: validating
# may run the reviewer again, documenting is the brief final-docs hop
# between reviewer approval and `in_review`, in_review is parked waiting
# for the HITL ready-ping and the human's manual merge, and fixing is
# between in_review and validating while a PR feedback round is being
# addressed. Documenting only checks ahead/behind vs. the PR branch
# (not the base) itself, so
# without this detour a sibling-PR merge during the docs pass would
# leave the docs commit on a stale base and only the next in_review
# tick would catch it; including the label here means only the
# `hold_base_sync` control label gates a PR-stage worktree's auto-
# rebase. `resolving_conflict` itself is excluded -- the handler runs
# this tick regardless and will do the rebase anyway. Other labels
# mean either no PR yet (pre-PR path applies instead) or terminal
# (done/rejected, nothing to refresh).
_PR_REFRESH_DETOUR_LABELS = frozenset(
    {"validating", "documenting", "in_review", "fixing"},
)


def _sync_worktree_with_base(
    gh: GitHubClient, spec: RepoSpec, worktree: Path, issue_number: int,
) -> None:
    """Bring a single per-issue worktree up to date with `origin/<base>`.

    Pre-PR: rebase onto `origin/<base>` directly. PR-having + behind base +
    label in {validating, documenting, in_review, fixing}: detour the
    issue to
    `resolving_conflict` so the existing handler does rebase + push +
    relabel back to `validating` (every pushed conflict-resolution path
    hands straight back to `validating` so the reviewer re-runs against
    the rebased branch directly; docs do not run here, the single docs
    pass runs after reviewer approval before `in_review` via the
    final-docs handoff to `documenting` in `_handle_validating`) in one
    consistent flow. Skips a dirty worktree
    or a worktree already up to date (no pre-PR rebase attempted, no PR
    detour fired). On a pre-PR content conflict, aborts the rebase so
    the worktree stays on its pre-rebase SHA -- conflict resolution
    lives in `_handle_resolving_conflict`, not here.
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

    if _worktree_dirty_files(worktree):
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
    if behind == 0:
        return

    if pr_number is not None:
        _route_pr_worktree_to_resolving_conflict(
            gh, spec, issue, state, int(pr_number), behind,
        )
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


def _route_pr_worktree_to_resolving_conflict(
    gh: GitHubClient,
    spec: RepoSpec,
    issue: Issue,
    state: PinnedState,
    pr_number: int,
    behind: int,
) -> None:
    """Flip a behind-base PR-having issue to `resolving_conflict`.

    Mirrors `_handle_in_review`'s unmergeable detour, just driven by a
    base advance instead of a PyGithub `mergeable=False`. The handler
    then runs `git rebase origin/<base>` in the worktree, pushes, and
    relabels back to `validating` so the reviewer re-runs against the
    rebased branch directly (a base-up-to-date no-op with no diff
    targets `validating` too). Docs do not run here -- the single docs
    pass runs after reviewer approval before `in_review` via the
    final-docs handoff to `documenting` in `_handle_validating`. This is the only safe pattern for PR-having
    worktrees, since a local-only rebase would diverge local HEAD from
    `pr.head.sha` and break every downstream gate that compares the
    two. This is the only auto-rebase path for PR-having worktrees --
    `_handle_in_review` is permanently manual-merge-only and just parks
    awaiting human attention on an unmergeable PR otherwise.

    Skips the detour when:

    * The label is not one this refresh knows how to drive into
      `resolving_conflict` (only `validating` / `documenting` /
      `in_review` / `fixing`);
      the `resolving_conflict` label itself is also skipped because the
      handler runs this tick anyway and will do the rebase regardless.

    * `awaiting_human=True`. `_handle_resolving_conflict`'s awaiting-human
      branch returns early without rebasing unless a new human comment
      arrived; relabeling here would just hide the existing park behind a
      `resolving_conflict` label without making any progress, including
      the documented `in_review` unmergeable park path. The park
      already invites the human to comment, and once they do, the existing
      handler's resume-on-human-reply branch picks the work up. Auto-
      unparking here would also undermine `_handle_validating`'s
      `MAX_REVIEW_ROUNDS` / `_handle_resolving_conflict`'s
      `MAX_CONFLICT_ROUNDS` caps, which exist precisely to require human
      intervention after repeated failures.

    * The issue has `hold_base_sync`, which is an explicit operator hold for
      series work where base should be integrated once after prerequisite PRs
      land, not after every intermediate base advance.

    * The PR is no longer open. A merged PR advances `origin/<base>`, so
      the still-validating / still-in_review / still-fixing worktree pointed
      at the now-stale branch is naturally behind base; without this gate
      the detour
      would post an "auto-resolution" notice and relabel to
      `resolving_conflict` on a PR the next handler call would finalize to
      `done`. Same for closed-without-merge if base advanced concurrently
      (handler would finalize to `rejected`). Leave terminal PR state to
      the existing stage logic. A `gh.get_pr` failure is treated as
      "leave it alone" -- the handler can retry on the next tick from a
      stable label rather than racing a half-known PR state from refresh.

    The watermark bump in `_handle_in_review`'s analogous detour is
    deliberately NOT replicated here. That bump is safe in_review-side
    because `_handle_in_review` has already scanned new comments before
    the relabel (anything past the watermark has been consumed by the
    fix-loop or filtered as orchestrator-authored). The refresh-time
    detour runs BEFORE any handler scans comments, so `latest_comment_id`
    may include unread human "do not merge" / fix-request comments;
    advancing the watermark here would silently mark them consumed and
    later validation / merge would skip them. The orchestrator's own
    PR notice we just posted is filtered out via `orchestrator_comment_ids`
    on the next `_handle_in_review` scan, so leaving the watermark alone
    does not cause the orchestrator to "see" its own message as fresh
    feedback.
    """
    label = gh.workflow_label(issue)
    if label not in _PR_REFRESH_DETOUR_LABELS:
        log.debug(
            "issue=#%d behind %s/%s by %d but label=%r; not detouring",
            issue.number, spec.remote_name, spec.base_branch, behind, label,
        )
        return

    if state.get("awaiting_human"):
        log.debug(
            "issue=#%d behind %s/%s by %d but awaiting_human=True; "
            "leaving park intact rather than relabeling without progress",
            issue.number, spec.remote_name, spec.base_branch, behind,
        )
        return

    if issue_has_label(issue, BASE_SYNC_HOLD_LABEL):
        log.debug(
            "issue=#%d behind %s/%s by %d but has %r; not detouring",
            issue.number, spec.remote_name, spec.base_branch, behind,
            BASE_SYNC_HOLD_LABEL,
        )
        return

    try:
        pr = gh.get_pr(pr_number)
    except Exception:
        log.debug(
            "issue=#%d could not fetch PR #%d for refresh detour; "
            "leaving label alone, handler will retry next tick",
            issue.number, pr_number,
        )
        return
    pr_status = gh.pr_state(pr)
    if pr_status != "open":
        # Merged / closed PR: the next handler call finalizes to done /
        # rejected. The base advance that put us "behind" is exactly the
        # merge that closed this PR -- there is nothing to auto-resolve.
        log.debug(
            "issue=#%d PR #%d is %s; not detouring (handler will finalize)",
            issue.number, pr_number, pr_status,
        )
        return

    log.info(
        "issue=#%d behind %s/%s by %d commit(s); routing %r -> "
        "resolving_conflict so the handler can rebase, push, and re-review",
        issue.number, spec.remote_name, spec.base_branch, behind, label,
    )

    # Match `_handle_in_review`'s seeding: only initialize `conflict_round`
    # when absent, so a re-entry preserves the cap counter and a
    # perpetually-stuck PR can't ping-pong between handlers indefinitely.
    if state.get("conflict_round") is None:
        state.set("conflict_round", 0)

    try:
        _post_pr_comment(
            gh, pr_number, state,
            f":mag: PR is {behind} commit(s) behind "
            f"`{spec.remote_name}/{spec.base_branch}`; "
            "orchestrator is attempting auto-resolution by rebasing "
            "the branch (label: `resolving_conflict`).",
        )
    except Exception:
        log.exception(
            "issue=#%s could not post auto-rebase notice to PR #%s",
            issue.number, pr_number,
        )

    gh.emit_event(
        "conflict_round",
        issue_number=issue.number,
        stage=label,
        pr_number=pr_number,
        sha=getattr(pr.head, "sha", None) or None,
        action="entered",
        conflict_round=int(state.get("conflict_round") or 0),
        review_round=int(state.get("review_round") or 0),
        retry_count=state.get("retry_count"),
    )
    gh.set_workflow_label(issue, "resolving_conflict")
    gh.write_pinned_state(issue, state)
