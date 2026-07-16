# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Documenting stage handler.

The documenting stage runs exactly once per reviewer-approval handoff,
between reviewer approval and `in_review`: after the reviewer agent
emits `VERDICT: APPROVED` and `_handle_validating` finishes the
local-verify + squash + watermark seed, it relabels to `documenting`.
The docs pass commits any README / docs edits, pushes them, and
advances to `in_review`. The `plans/` tree and roadmap entries are
deliberately out of scope: those are working notes owned by humans, so
the docs prompt instructs the agent to compare only against `README.md`
and the `docs/` tree. A PR can therefore visit `documenting`
more than once over its life: if PR feedback later bounces the issue
to `fixing` and the dev pushes a fix, the next reviewer approval
triggers another final-docs pass before the next `in_review` handoff.
There is no pre-approval entry: every `_handle_implementing` PR open,
every pushed dev fix in `_handle_validating` / `_handle_fixing` /
`_handle_in_review`'s drift exit, and every `_handle_resolving_conflict`
pushed exit hand straight back to `validating` so the reviewer re-runs
against the new branch.

Locking and session semantics mirror `implementing`'s dev role: the
documentation pass operates AS the developer (it commits to the dev's
branch), so it shares the dev session id and backend recorded in pinned
state. A locked-backend resume is used for any human reply that
follows a park.

Outcomes the handler distinguishes:
  * A fresh docs commit landed on the worktree (any subject -- the prompt
    no longer mandates a `docs:` prefix) -> push + advance to
    `in_review`.
  * The agent emitted the explicit `DOCS: NO_CHANGE` marker against a
    remote-clean head -> persist the verdict, post a one-liner, advance
    to `in_review` without pushing.
  * No commit and no marker -> park awaiting human via `_on_question`.
  * Timeout / dirty worktree / push failure -> park with the same
    `park_reason` tokens implementing and validating use.
  * User-content drift mid-hop -> the prior approval was for stale
    requirements, so the handler resets `review_round=0` and relabels
    back to `validating` without spawning the docs agent. The reviewer
    re-evaluates the updated body on the next tick.

Restart idempotency: on re-entry the helper reuses the existing PR
worktree. If the worktree carries commits ahead of `<remote>/<branch>`
from a previous tick whose push failed, those commits are pushed and
the issue advances without re-spawning the agent.

Open `documenting` issues touch only their own pinned state and
worktree, so the label is deliberately NOT listed in
`workflow._FAMILY_AWARE_LABELS` and `tick()` routes it through the
fan-out bucket.

`_handle_documenting` is a thin router over stage-private helpers. The
per-tick handles, resolved branch, and pinned `pr_number` travel together
in a frozen `_DocumentingContext` (mirrors fixing's `_FixingContext`), and
the docs run's outcome in a frozen `_DocumentingRun`. The router chains
terminal short-circuits (`_finalize_documenting_terminal`,
`_park_documenting_without_pr`), the `/orchestrator continue` refusal
(`_refuse_parked_continue_command`), drift detection + unwind
(`_reconcile_documenting_drift`, `_reset_documenting_drift_worktree`), the
parked-no-input fast path (`_documenting_parked_no_input`), the worktree
prep + docs run (`_drive_documenting_pass` over
`_prepare_documenting_worktree` and `_run_documenting_dev`), and the
post-agent disposition (`_dispose_documenting_outcome`,
`_push_docs_and_advance`, `_route_documenting_no_change`). None of these
are re-exported from `workflow.py`; they are private to this module.

ALL workflow-owned helpers (`_park_awaiting_human`, `_run_agent_tracked`,
`_now_iso`, the worktree plumbing, the docs prompt + verdict parser
re-exported into `workflow`) are reached through the parent module via
`from .. import workflow as _wf` at call time. Tests patch the
compatibility surface as `patch.object(workflow, "_foo")`, so the
handler must NOT direct-import those names from
`workflow_messages` / `worktrees`; binding a stable reference would
defeat the patch.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from github.Issue import Issue

from orchestrator import config
from orchestrator.agents import AgentResult
from orchestrator.comment_trust import filter_trusted
from orchestrator.config import RepoSpec
from orchestrator.state_machine import WorkflowLabel
from orchestrator.github import GitHubClient, PinnedState


@dataclass(frozen=True)
class _DocumentingContext:
    """The per-tick `documenting` invocation handles plus the resolved
    `branch` and pinned `pr_number`, bundled so the drift-unwind,
    worktree-prep, docs run, and disposition helpers thread them as a single
    value instead of up to six positional arguments (mirrors fixing's
    `_FixingContext`). `branch` and `pr_number` are tick-invariant once
    `_handle_documenting`'s missing-`pr_number` guard has passed, so every
    consumer downstream of the guards reads them off the context.
    """
    gh: GitHubClient
    spec: RepoSpec
    issue: Issue
    state: PinnedState
    branch: str
    pr_number: Any


@dataclass(frozen=True)
class _DocumentingRun:
    """The outcome of one documenting attempt: the worktree the pass ran in,
    the agent result, the HEAD before the run, whether it was the
    recovered-commit shortcut (no agent spawned), whether an operator paused
    mid-run, and the worktree's ahead count vs. `<remote>/<branch>`. `ahead`
    is threaded to the disposition so a no-change verdict over a recovered
    commit still pushes it.
    """
    worktree: Any
    agent_result: AgentResult
    before_sha: str
    recovered: bool
    paused: bool
    ahead: int


def _park_documenting(
    ctx: _DocumentingContext, message: str, reason: str,
) -> None:
    """Park the docs pass awaiting a human and re-stamp the durable
    `park_reason`.

    `_park_awaiting_human` clears `park_reason` by contract; re-set the
    durable tag so future ticks / dashboards can branch on it -- documenting's
    awaiting-human resume also reads it to distinguish stale park flags after
    a relabel. Writes pinned state; the caller returns unconditionally.
    """
    from orchestrator import workflow as _wf

    _wf._park_awaiting_human(
        ctx.gh, ctx.issue, ctx.state, message, reason=reason,
    )
    ctx.state.set("park_reason", reason)
    ctx.gh.write_pinned_state(ctx.issue, ctx.state)


def _ratchet_in_review_watermark_for_final_docs(
    gh: GitHubClient, issue: Issue, state: PinnedState,
) -> None:
    """Ratchet `pr_last_comment_id` past issue-thread comments the docs
    pass already consumed during the final-docs hop.

    During documenting's awaiting-human resume the handler advances
    `last_action_comment_id` past the human reply it fed into the
    `_build_documentation_prompt` resume. The final-docs handoff then
    relabels to `in_review`, which scans `comments_after(issue,
    pr_last_comment_id)` and falls back to `last_action_comment_id`
    only when `pr_last_comment_id is None`. Without this ratchet a
    `pr_last_comment_id` validating seeded BEFORE the human's reply
    keeps the older value, the consumed reply replays as fresh PR
    feedback, and in_review bounces the issue to `fixing` over work
    the dev has already addressed.

    Reuse `_latest_pr_comment_ids` (the same seed-walk validating uses
    at its approval handoff) so a PR-conversation comment with id
    between the prior `pr_last_comment_id` and the consumed-through
    threshold is NOT swallowed -- the walk stops at the first unread
    non-orchestrator comment on either surface. `consumed_through` is
    applied to the issue thread only inside the walk, which is what
    keeps PR-conversation feedback visible to in_review's
    fresh-feedback scan. Ratchets via `max` so a previous in_review
    tick's higher watermark is never regressed.

    A PR fetch failure is treated as best-effort: log and skip, so the
    docs handoff itself still advances. In the worst case in_review
    will route to `fixing` and the rescan there is debounced and
    correct on its own.
    """
    from orchestrator import workflow as _wf

    pr_number = state.get("pr_number")
    if pr_number is None:
        return
    try:
        pr = gh.get_pr(int(pr_number))
    except Exception as error:
        _wf.log.warning(
            "issue=#%s could not fetch PR #%s to ratchet "
            "`pr_last_comment_id` on the final-docs handoff: %s",
            issue.number, pr_number, error,
        )
        return

    candidate, _ = _wf._latest_pr_comment_ids(gh, issue, pr, state)
    prev_wm = state.get("pr_last_comment_id")
    if isinstance(prev_wm, int):
        candidate = (
            prev_wm if candidate is None
            else max(candidate, prev_wm)
        )
    if candidate is None:
        return
    state.set("pr_last_comment_id", candidate)


def _advance_after_docs_push(
    gh: GitHubClient, issue: Issue, state: PinnedState,
) -> None:
    """Route the issue forward after a successful docs push.

    Advance to `in_review` -- the approval comment, squash comment, and
    PR watermarks set by validating remain on state untouched, with the
    in-review issue-comment watermark ratcheted past anything the
    awaiting-human resume already consumed.
    """
    _ratchet_in_review_watermark_for_final_docs(gh, issue, state)
    gh.set_workflow_label(issue, WorkflowLabel.IN_REVIEW)


def _advance_after_docs_no_change(
    gh: GitHubClient, issue: Issue, state: PinnedState,
) -> None:
    """Route the issue forward after a clean no-change docs verdict.

    No commit landed, so the PR head is unchanged. Ratchet the in-review
    issue-comment watermark past any issue-thread reply the
    awaiting-human resume already consumed, and advance to `in_review`.
    """
    _ratchet_in_review_watermark_for_final_docs(gh, issue, state)
    gh.set_workflow_label(issue, WorkflowLabel.IN_REVIEW)


def _finalize_documenting_terminal(
    gh: GitHubClient, spec: RepoSpec, issue: Issue, state: PinnedState,
) -> bool:
    """Terminal issue/PR short-circuits before the docs pass runs.

    External merge: if the PR was merged before the docs pass ran,
    finalize to `done` rather than fetching the branch and running the
    documenting agent against an already-landed PR. Closed-issue
    counterpart: the closed-`documenting` sweep yields issues a human
    closed without a merged PR -- flip to `rejected` so the docs agent
    does not run against a closed issue.

    Returns True when the issue was routed to a terminal state and the
    caller must return.
    """
    from orchestrator import workflow as _wf

    if _wf._finalize_if_pr_merged(gh, spec, issue, state):
        return True
    if _wf._finalize_if_issue_closed(gh, spec, issue, state):
        return True
    return False


def _park_documenting_without_pr(
    gh: GitHubClient, issue: Issue, state: PinnedState,
) -> None:
    """Park a `documenting` issue that has no pinned `pr_number`.

    Documenting only runs against an existing PR worktree. Without a
    pinned `pr_number` we cannot anchor on the dev's branch and must not
    branch off the base (that would orphan the docs commit from the
    implementing PR). Park once and let the operator relabel; idempotency
    by `awaiting_human` mirrors `_handle_in_review`'s missing-pr-number
    guard.
    """
    from orchestrator import workflow as _wf

    if state.get("awaiting_human"):
        return
    _wf._park_awaiting_human(
        gh, issue, state,
        f"{config.HITL_MENTIONS} `documenting` without a pinned "
        "`pr_number`; the documenting stage runs against an existing "
        "PR worktree. Relabel back to `implementing` (the dev's PR "
        "opens there) after fixing.",
        reason="missing_pr_number",
    )
    gh.write_pinned_state(issue, state)


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
        _park_documenting(
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
        try:
            return int(parts[1]), int(parts[0])
        except ValueError:
            pass
    _wf.log.error(
        "issue=#%d documenting drift ahead/behind probe "
        "failed (rc=%s stderr=%s stdout=%s)",
        ctx.issue.number, probe.returncode,
        (probe.stderr or "").strip(),
        (probe.stdout or "").strip(),
    )
    _park_documenting(
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
        _park_documenting(
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
        _park_documenting(
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

    if not _documenting_drift_fetch(ctx, wt):
        return False
    probe = _documenting_drift_probe(ctx, wt)
    if probe is None:
        return False
    ahead, behind = probe
    dirty = _wf._worktree_dirty_files(wt)
    if ahead > 0 or behind > 0 or dirty:
        return _documenting_drift_hard_reset(ctx, wt)
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
    state.set("awaiting_human", False)
    state.set("park_reason", None)
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
    if pending_unwind and not fresh_drift and ctx.state.get("awaiting_human"):
        last_action_id = ctx.state.get("last_action_comment_id")
        if not filter_trusted(ctx.gh.comments_after(ctx.issue, last_action_id)):
            return True
    if not (fresh_drift or pending_unwind):
        return False

    if fresh_drift:
        _announce_documenting_drift(ctx, new_hash)
    _begin_documenting_drift_unwind(ctx)
    wt = _wf._worktree_path(ctx.spec, ctx.issue.number)
    if wt.exists() and not _reset_documenting_drift_worktree(ctx, wt):
        return True
    # Reconcile succeeded (or the worktree didn't exist): the drift unwind is
    # complete, clear the sentinel and relabel.
    ctx.state.set("docs_drift_unwind_pending", False)
    ctx.gh.set_workflow_label(ctx.issue, WorkflowLabel.VALIDATING)
    ctx.gh.write_pinned_state(ctx.issue, ctx.state)
    return True


def _documenting_parked_no_input(
    gh: GitHubClient, issue: Issue, state: PinnedState,
) -> bool:
    """Already-parked, no-new-input fast path.

    When `awaiting_human` is set and no human comment has arrived since
    the park (and drift did not clear the flag), there is nothing to act
    on. Skip the fetch + ahead/behind check entirely so a transient
    failure mode (fetch_failed / diverged_branch) does NOT re-post its
    park comment every tick -- non-recoverable parks (agent_question /
    dirty_worktree / agent_silent) likewise stay silent until a human
    reply. Validating uses the same shape via its transient-park recovery
    branch; documenting has no transient recovery yet, so the early
    return alone is enough.

    Returns True when the issue is parked with nothing to act on (the
    caller must return), False to proceed with the normal docs flow.
    """
    from orchestrator import workflow as _wf

    if not state.get("awaiting_human"):
        return False
    # The refresh-time `_AUTO_REBASE_PARK_REASONS` parks belong to the
    # `_sync_pr_worktree_to_base` retry loop -- the operator's new comment
    # is the "retry the rebase" signal, NOT a documenting-stage trigger.
    # Stay silent so the refresh keeps ownership of the comment.
    if state.get("park_reason") in _wf._AUTO_REBASE_PARK_REASONS:
        return True
    last_action_id = state.get("last_action_comment_id")
    # Only a trusted reply wakes a parked docs pass: with `ALLOWED_ISSUE_AUTHORS`
    # set an outsider comment must read as silence so the park survives instead
    # of falling through to the docs resume in `_run_documenting_dev`.
    if not filter_trusted(gh.comments_after(issue, last_action_id)):
        return True
    return False


def _prepare_documenting_worktree(ctx: _DocumentingContext, wt):
    """Refresh `<remote>/<branch>` and guard against a diverged worktree.

    Refresh the remote-tracking ref BEFORE the ahead/behind check. A
    stale local remote-tracking ref would mis-classify a "remote moved
    out from under us" situation as in-sync, and the eventual
    `_push_branch` (which uses `--force-with-lease` against the local
    view of the remote) would clobber the real PR head. Mirrors the
    fetch-then-check pattern in `_handle_resolving_conflict`.

    Returns the worktree's ahead count vs. `<remote>/<branch>` on success,
    or None when a fetch failure or diverged worktree parked the issue
    (the caller must return).
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
            "issue=#%d documenting branch fetch failed: %s",
            ctx.issue.number, (fetch_branch.stderr or "").strip(),
        )
        _park_documenting(
            ctx,
            f"{config.HITL_MENTIONS} `git fetch {spec.remote_name} "
            f"{branch}` failed during documenting; see orchestrator logs.",
            "fetch_failed",
        )
        return None

    ahead, behind = _wf._branch_ahead_behind(spec, wt, branch)
    if behind > 0:
        # Stale or diverged worktree. The reviewer's PR head has commits
        # we never saw, so pushing local state (even a clean recovery
        # push) would overwrite them. Refuse to act -- the same shape
        # `_handle_resolving_conflict`'s diverged-branch guard uses.
        _park_documenting(
            ctx,
            f"{config.HITL_MENTIONS} worktree on `{branch}` is {ahead} "
            f"ahead and {behind} behind `{spec.remote_name}/{branch}`; "
            "refusing to push a stale documenting branch over the "
            "real PR head. Manual intervention needed.",
            "diverged_branch",
        )
        return None

    return ahead


def _documentation_prompt(ctx: _DocumentingContext) -> str:
    """Build the FULL documentation prompt (issue body + recent comments +
    the `DOCS: NO_CHANGE` marker contract) shared by the resume and fresh
    docs runs."""
    from orchestrator import workflow as _wf

    return _wf._build_documentation_prompt(
        ctx.spec, ctx.issue, _wf._recent_comments_text(ctx.issue),
        config.default_repo_specs(),
    )


def _resume_documenting_dev(ctx: _DocumentingContext, wt, ahead: int):
    """Awaiting-human resume: rerun the FULL documentation prompt.

    The generic `_resume_developer_on_human_reply` helper builds the followup
    from ONLY the new human comments, which is the right shape for
    implementing/validating (the dev has an in-context docs spec already) but
    wrong for documenting: a `fetch_failed` / `agent_timeout` / `agent_silent`
    resume may be the FIRST time this session sees the docs-stage instructions
    (the DOCS: NO_CHANGE marker, what files to inspect, what to commit).
    Without those, the dev could emit a stray `DOCS: NO_CHANGE` it learned
    from an earlier spawn and the issue would advance to validating without
    ever running a real docs pass. `_build_documentation_prompt` quotes the
    issue body AND the full conversation via `_recent_comments_text`, so the
    human's latest reply is naturally included.

    Returns a `_DocumentingRun`, or None when there is no new trusted comment
    and the tick should end without disposition.
    """
    from orchestrator import workflow as _wf

    # Drop untrusted authors before the resume signal / watermark advance:
    # with `ALLOWED_ISSUE_AUTHORS` set an outsider reply must not resume the
    # docs pass NOR advance the consumed watermark. Only trusted comments are
    # consumed, so an outsider reply trailing a trusted one is left unconsumed;
    # an all-untrusted batch reads as "no new reply".
    new_comments = filter_trusted(
        ctx.gh.comments_after(ctx.issue, ctx.state.get("last_action_comment_id")),
    )
    if not new_comments:
        return None
    ctx.state.set(
        "last_action_comment_id", max(comment.id for comment in new_comments),
    )
    # Anchor `before_sha` from the just-fetched PR worktree BEFORE the resume
    # so the post-spawn check sees a real difference if (and only if) the
    # resumed dev produced a new commit. Persist `docs_checked_sha` BEFORE the
    # spawn for the same reason the fresh-spawn shape does: a no-change verdict
    # on this resume relies on this watermark to identify the confirmed commit.
    before_sha = _wf._head_sha(wt)
    ctx.state.set("docs_checked_sha", before_sha or "")
    wt, documentation_result, paused = _wf._resume_dev_with_text(
        ctx.gh, ctx.spec, ctx.issue, ctx.state, _documentation_prompt(ctx),
        followup_has_tracked_repos=True,
        pause_guard=True,
    )
    return _DocumentingRun(wt, documentation_result, before_sha, False, paused, ahead)


def _recovered_documenting_run(ctx: _DocumentingContext, wt, ahead: int):
    """Recovered worktree: a previous tick committed docs but crashed before
    the push. Synthesize a non-interrupted result and skip the agent spawn so
    the unified commit/dirty/push disposition ships it.

    An uncommitted file left alongside the recovered commit still parks via
    `_on_dirty_worktree` instead of being silently dropped by the push (which
    only ships staged work). A drift event this tick would have routed back to
    `validating` before reaching this shape, so the recovered commit is always
    against the still-valid approved body. Empty `before_sha` makes the
    post-spawn check treat the recovered HEAD as a fresh commit.
    """
    from orchestrator import workflow as _wf

    _wf.log.info(
        "issue=#%d documenting: %d recovered docs commit(s); "
        "skipping agent spawn and pushing",
        ctx.issue.number, ahead,
    )
    _, _, _, dev_sid = _wf._read_dev_session(ctx.state)
    documentation_result = AgentResult(
        session_id=dev_sid,
        last_message=(
            "(orchestrator restart: pushing previously committed docs)"
        ),
        exit_code=0,
        timed_out=False,
        stdout="",
        stderr="",
    )
    # No agent ran this tick (dispatch already gated the label at tick start),
    # so there is no live-pause window to observe here.
    return _DocumentingRun(wt, documentation_result, "", True, False, ahead)


def _fresh_documenting_run(ctx: _DocumentingContext, wt, ahead: int):
    """Fresh docs pass: snapshot `before_sha`, persist the pre-spawn
    watermarks, and resume the dev session with the docs prompt.

    Resume the dev session through the shared helper (rather than a bare
    `_run_agent_tracked`) so the initial docs pass participates in dev-session
    rotation (`DEV_SESSION_MAX_RESUMES`) and immediate Claude context-overflow
    recovery, exactly like the awaiting-human shape. A direct resume replays
    the whole transcript every tick without charging the resume budget, so a
    long-lived session could overflow on the final docs pass without ever
    rotating. Persist the spec so a backend hiccup that yields no session id
    still leaves a durable role-identity record; matches
    `_handle_implementing`'s fresh-spawn branch.
    """
    from orchestrator import workflow as _wf

    before_sha = _wf._head_sha(wt)
    ctx.state.set("docs_checked_sha", before_sha or "")
    dev_spec, _, _, _ = _wf._read_dev_session(ctx.state)
    ctx.state.set("dev_agent", dev_spec)
    wt, documentation_result, paused = _wf._resume_dev_with_text(
        ctx.gh, ctx.spec, ctx.issue, ctx.state, _documentation_prompt(ctx),
        followup_has_tracked_repos=True,
        pause_guard=True,
    )
    ctx.state.set("branch", ctx.branch)
    return _DocumentingRun(wt, documentation_result, before_sha, False, paused, ahead)


def _run_documenting_dev(ctx: _DocumentingContext, wt, ahead: int):
    """Run the docs pass and return its `_DocumentingRun` for disposition.

    Three entry shapes, in priority order:
      * awaiting-human resume -> rerun the FULL documentation prompt.
      * recovered worktree (`ahead > 0`) -> synthesize a non-interrupted
        result for previously-committed docs and skip the agent spawn.
      * fresh spawn -> resume the dev session with the docs prompt.

    Returns a `_DocumentingRun`, or None when an awaiting-human resume finds
    no new comments and the tick should end without disposition.
    """
    if ctx.state.get("awaiting_human"):
        return _resume_documenting_dev(ctx, wt, ahead)
    if ahead > 0:
        return _recovered_documenting_run(ctx, wt, ahead)
    return _fresh_documenting_run(ctx, wt, ahead)


def _stamp_docs_verdict(
    state: PinnedState, checked_sha: str, verdict: str,
) -> None:
    """Stamp the docs watermarks after a terminal success: record the
    evaluated head, the verdict (`updated` / `no_change`), and reset the
    silent-park counter."""
    state.set("docs_checked_sha", checked_sha)
    state.set("docs_verdict", verdict)
    state.set("silent_park_count", 0)


def _post_docs_notice(ctx: _DocumentingContext, note: str) -> None:
    """Post a docs-pass notice on the PR, best-effort (a comment failure must
    not block the handoff)."""
    from orchestrator import workflow as _wf

    try:
        _wf._post_pr_comment(ctx.gh, int(ctx.pr_number), ctx.state, note)
    except Exception:
        _wf.log.exception(
            "issue=#%s could not post docs notice to PR #%s",
            ctx.issue.number, ctx.pr_number,
        )


def _push_docs_and_advance(
    ctx: _DocumentingContext, wt, after_sha: str, notice: str,
) -> None:
    """Push docs commit(s) and hand off to `in_review`.

    On push failure, park with `push_failed` instead of advancing. On
    success, stamp the docs watermarks (`docs_checked_sha`,
    `docs_verdict=updated`), post `notice` on the PR, and route to
    `in_review`. Writes pinned state; the caller returns unconditionally.
    """
    from orchestrator import workflow as _wf

    if not _wf._push_branch(ctx.spec, wt, ctx.branch):
        _park_documenting(
            ctx,
            f"{config.HITL_MENTIONS} git push failed; see "
            "orchestrator logs.",
            "push_failed",
        )
        return
    _stamp_docs_verdict(ctx.state, after_sha, "updated")
    _post_docs_notice(ctx, notice)
    _advance_after_docs_push(ctx.gh, ctx.issue, ctx.state)
    ctx.gh.write_pinned_state(ctx.issue, ctx.state)


def _documenting_no_change_note(body: str) -> str:
    """Build the `DOCS: NO_CHANGE` PR notice, quoting the agent's
    justification when it supplied one."""
    justification = body.strip()
    base = ":books: documenting pass: no docs changes required."
    if not justification:
        return base
    quoted = "> " + justification.replace("\n", "\n> ")
    return f"{base}\n\n{quoted}"


def _route_documenting_no_change(
    ctx: _DocumentingContext, wt, ahead: int, after_sha: str, body: str,
) -> None:
    """Route a `DOCS: NO_CHANGE` verdict to `in_review`.

    A recovered local commit (`ahead > 0`) that the resumed dev added
    nothing to must still reach the remote before advancing -- otherwise
    the reviewer agent at validating would never see the docs in the diff
    -- so push it via the updated path. Otherwise persist the clean
    no-change verdict against the evaluated head and advance. Writes
    pinned state; the caller returns unconditionally.
    """
    if ahead > 0:
        _push_docs_and_advance(
            ctx, wt, after_sha,
            ":books: documenting pass: pushed recovered docs "
            "commit(s) after no-change confirmation.",
        )
        return
    # Persist the SHA the dev evaluated even on a "nothing changed" outcome.
    # The fresh-spawn and awaiting-human resume shapes both write
    # `docs_checked_sha = before_sha` BEFORE the spawn (so a no-change outcome
    # there leaves it correct); setting it here too makes the post-condition
    # explicit and covers any future entry path that bypasses them.
    # `after_sha == before_sha` in this branch by construction (no commit).
    _stamp_docs_verdict(ctx.state, after_sha, "no_change")
    _post_docs_notice(ctx, _documenting_no_change_note(body))
    _advance_after_docs_no_change(ctx.gh, ctx.issue, ctx.state)
    ctx.gh.write_pinned_state(ctx.issue, ctx.state)


def _documenting_commit_notice(recovered: bool) -> str:
    """The `:books:` push notice, distinguishing a recovered commit from a
    fresh docs commit."""
    if recovered:
        return ":books: documenting pass: pushed recovered docs commit(s)."
    return ":books: documenting pass: pushed docs commit."


def _park_documenting_dirty(
    ctx: _DocumentingContext, documentation_result: AgentResult, dirty,
) -> None:
    """Park an uncommitted docs edit via `_on_dirty_worktree`; writes pinned
    state."""
    from orchestrator import workflow as _wf

    _wf._on_dirty_worktree(
        ctx.gh, ctx.issue, ctx.state, documentation_result, dirty,
    )
    ctx.gh.write_pinned_state(ctx.issue, ctx.state)


def _park_documenting_question(
    ctx: _DocumentingContext, documentation_result: AgentResult,
) -> None:
    """Park an unknown verdict via `_on_question`.

    `_on_question` posts the HITL ping, distinguishes the silent-crash case
    via stderr diagnostics, and tags `silent_park_count` so a poisoned session
    can be dropped on the next resume. Writes pinned state.
    """
    from orchestrator import workflow as _wf

    _wf._on_question(ctx.gh, ctx.issue, ctx.state, documentation_result)
    ctx.gh.write_pinned_state(ctx.issue, ctx.state)


def _dispose_documenting_clean(
    ctx: _DocumentingContext, wt, ahead: int, after_sha: str,
    documentation_result: AgentResult,
) -> None:
    """No new commit on a clean tree: the agent either declared no change or
    asked a question. The explicit `DOCS: NO_CHANGE` marker is the only signal
    that confirms the diff was checked and nothing was needed; anything else
    parks via `_on_question`."""
    from orchestrator import workflow as _wf

    verdict, body = _wf._parse_documentation_verdict(
        documentation_result.last_message or "",
    )
    if verdict == "no_change":
        _route_documenting_no_change(ctx, wt, ahead, after_sha, body)
        return
    _park_documenting_question(ctx, documentation_result)


def _dispose_documenting_outcome(
    ctx: _DocumentingContext, run: _DocumentingRun,
) -> None:
    """Route the post-agent outcome: timeout / dirty / commit / no-change
    / question.

    Writes pinned state on every terminal branch; the caller returns
    unconditionally.
    """
    from orchestrator import workflow as _wf

    if run.agent_result.timed_out:
        _park_documenting(
            ctx,
            f"{config.HITL_MENTIONS} agent timed out after "
            f"{config.AGENT_TIMEOUT}s, manual intervention needed.",
            "agent_timeout",
        )
        return

    wt = _wf._worktree_path(ctx.spec, ctx.issue.number)
    after_sha = _wf._head_sha(wt)

    # A dirty worktree blocks every downstream outcome -- commit + push would
    # publish a branch that omits the dirty files, and the no-change /
    # on_question paths would silently leave docs edits behind on disk that the
    # eventual reviewer never sees. Check before any other decision so an agent
    # that edited files without committing (and then either emitted
    # `DOCS: NO_CHANGE`, asked a question, or produced nothing) cannot slip past.
    dirty = _wf._worktree_dirty_files(wt)
    if dirty:
        _park_documenting_dirty(ctx, run.agent_result, dirty)
        return

    if after_sha and after_sha != run.before_sha:
        _push_docs_and_advance(
            ctx, wt, after_sha, _documenting_commit_notice(run.recovered),
        )
        return

    _dispose_documenting_clean(ctx, wt, run.ahead, after_sha, run.agent_result)


def _refuse_parked_continue_command(
    gh: GitHubClient, issue: Issue, state: PinnedState,
) -> bool:
    """Refuse a content-free `/orchestrator continue` on a `documenting` park
    that needs real human guidance, BEFORE the drift / resume paths.

    Documenting has no preserved feedback batch to replay, so a bare continue
    resolves to just two shapes: a retryable session-failure park
    (`agent_silent` / `agent_timeout`) whose awaiting-human resume reruns the
    FULL documentation prompt, and a park that needs a real answer. A bare
    continue no longer shifts `user_content_hash`, so `_reconcile_documenting_drift`
    stays silent and the retry falls through to `_run_documenting_dev`'s resume
    (issue #729) -- only the refusal needs interception here.

    Returns True when a content-free continue on a non-retryable park was
    refused (command consumed, note posted, state written) and the caller must
    return. Returns False to fall through: not parked, an auto-rebase park (the
    refresh loop owns the nudge), no new comment, no bare continue, a retryable
    park, or a command posted alongside genuine guidance.
    """
    from orchestrator import workflow as _wf

    if not state.get("awaiting_human"):
        return False
    park_reason = state.get("park_reason")
    if park_reason in _wf._AUTO_REBASE_PARK_REASONS:
        return False
    new_comments = filter_trusted(
        gh.comments_after(issue, state.get("last_action_comment_id"))
    )
    if not new_comments:
        return False
    if _wf._continue_command_action(new_comments, park_reason) != "refuse":
        return False
    _wf._refuse_parked_continue(gh, issue, state)
    gh.write_pinned_state(issue, state)
    return True


def _drive_documenting_pass(ctx: _DocumentingContext):
    """Prepare the worktree, run the docs pass, and return the run outcome.

    Returns a `_DocumentingRun` ready for disposition, or None when the tick
    is already fully handled and the caller must return without disposition:
    a fetch / diverged-branch park, an awaiting-human resume with no new
    comment, a shutdown-sweep interruption, or an operator pause.
    """
    from orchestrator import workflow as _wf

    wt = _wf._ensure_pr_worktree(ctx.spec, ctx.issue.number, branch=ctx.branch)

    ahead = _prepare_documenting_worktree(ctx, wt)
    if ahead is None:
        return None

    run = _run_documenting_dev(ctx, wt, ahead)
    if run is None:
        return None

    ctx.state.set("last_agent_action_at", _wf._now_iso())

    # Shutdown-sweep interruption: a docs run the orchestrator killed
    # mid-flight has no trustworthy result (the recovered `ahead > 0` shape
    # synthesizes its own non-interrupted result, so only a real resume /
    # fresh-docs spawn can land here). Ignore it and return WITHOUT writing
    # pinned state -- the pre-spawn `docs_checked_sha` / watermark mutations
    # are discarded so the next process re-runs the docs pass.
    if _wf._ignore_if_interrupted(ctx.issue, run.agent_result):
        return None

    # Live pause applied while the docs agent ran: honor the decision the
    # resume helper already made (the recovered `ahead > 0` shape ran no agent
    # and reports False). Stop before the disposition posts a PR comment,
    # pushes, advances to `in_review`, or writes pinned state. The committed
    # docs work stays on the branch and republishes through the
    # recovered-worktree path once the label is removed.
    if run.paused:
        return None

    return run


def _handle_documenting(gh: GitHubClient, spec: RepoSpec, issue: Issue) -> None:
    from orchestrator import workflow as _wf

    state = gh.read_pinned_state(issue)
    pr_number = state.get("pr_number")

    if _finalize_documenting_terminal(gh, spec, issue, state):
        return

    if pr_number is None:
        _park_documenting_without_pr(gh, issue, state)
        return

    # Operator `/orchestrator continue` on a park that needs real guidance:
    # refuse before the drift / resume paths. Retryable session-failure parks
    # and command-plus-guidance comments fall through -- a bare continue does
    # not shift `user_content_hash`, so the retryable resume reruns the docs
    # prompt without a spurious drift notice. See `_refuse_parked_continue_command`.
    if _refuse_parked_continue_command(gh, issue, state):
        return

    ctx = _DocumentingContext(
        gh, spec, issue, state,
        _wf._resolve_branch_name(state, spec, issue.number), pr_number,
    )

    if _reconcile_documenting_drift(ctx):
        return

    if _documenting_parked_no_input(gh, issue, state):
        return

    run = _drive_documenting_pass(ctx)
    if run is None:
        return

    _dispose_documenting_outcome(ctx, run)
