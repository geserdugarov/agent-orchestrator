# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Force-publishing one clean rebase, and the surfaces it then lands on.

The lease-pinned push is the commit point of the whole auto-rebase: until
the rewritten branch is on the remote, the head the reviewer votes on is
still the pre-rebase SHA. So every check that can still refuse -- an
unreadable HEAD, a rewrite that moved nothing, a tree that came back dirty
-- runs before it and hands off to ``guards``, and the lease itself is
pinned to that same pre-rebase SHA so a foreign update rejects the push
instead of being clobbered. Only an accepted push earns the tail: the PR
notice, the audit event, the `validating` relabel, and last the
`write_pinned_state` that commits the cleared anchor and reset review
round, so a tick that dies partway leaves the anchor pinned for the next
one to recover from.

The rebase is also a REWRITE of whatever the branch was standing on, and on
an issue whose exemption names that commit it is the rewrite that would
punish it: the exemption names one commit and only it, so the replayed
object is measured past the same ceiling and the change a human already
adjudicated goes back into adjudication with a pull request open over it.
So the gate is handed evidence beside the candidate, and ``transfers`` is
what assembles it -- the pair the exemption already records, the pair this
rebase produced, and the publication and pre-rebase anchor the push is made
against. That owner rather than this one, because the recovery that finishes
an interrupted rebase needs the same claim and has no rewrite of its own to
describe. Nothing on either road rules on it: `late_transfer` grants or
refuses the permit, and a refusal simply leaves the ordinary cumulative gate
to measure the rebase like any other candidate, which is what a base advance
that changed the contribution has to get.
"""
from __future__ import annotations

from orchestrator.git.base_sync import guards, persistence, transfers
from orchestrator.git.base_sync.models import _AutoRebaseContext
from orchestrator.git.base_sync.state import _REVIEW_ROUND, log
from orchestrator.git.verification import probes
from orchestrator.git.worktrees import paths
from orchestrator.workflow.state import WorkflowLabel, stage_name


def _gated_publication():
    """The size gate the rebase push passes, imported where it is used.

    A rebase onto a base that has moved changes what the branch adds to it, so
    the pull request can cross the ceiling with nobody having written a line.
    Lazily bound for the reason the comment owner beside it is: the gate sits
    in the workflow layer above this package, and binding it at module load
    would make every git-side import pay for the stage tree it pulls in.
    """
    from orchestrator.workflow.stages.implementing import late_push
    return late_push


def _gate_records():
    """The subject and terms one gated publication is described by.

    The gate's own record owner, reached the same way and for the same
    reason: what this package hands the gate is a subject built from the
    context it already holds, and building one costs the same upward hop the
    call does.
    """
    from orchestrator.workflow.stages.implementing import late_records
    return late_records


def _post_auto_rebase_notice(
    context: _AutoRebaseContext,
    after_sha: str,
) -> None:
    """Post the best-effort PR notice for a published clean rebase."""
    # Lazy import: the comment owner sits in the workflow layer above this
    # package, so binding it at module load would make every git-side
    # import pay for the GitHub client and prompt state it pulls in.
    from orchestrator.workflow.engine import comments as _comments
    spec = context.spec
    after_short = after_sha[:8]
    try:
        _comments._post_pr_comment(
            context.gh,
            context.pr_number,
            context.state,
            f":mag: PR was {context.behind} commit(s) behind "
            f"`{spec.remote_name}/{spec.base_branch}`; "
            "orchestrator auto-rebased the branch and re-pushed it. "
            f"Routing `{context.label}` -> `{WorkflowLabel.VALIDATING}` so "
            f"the reviewer re-runs against the new head (`{after_short}`).",
        )
    except Exception:  # noqa: BLE001 - the PR notice is best effort at the GitHub boundary
        log.exception(
            "issue=#%s could not post auto-rebase notice to PR #%s",
            context.issue.number,
            context.pr_number,
        )


def _emit_auto_rebase_event(
    context: _AutoRebaseContext,
    after_sha: str,
) -> None:
    """Emit the stable audit shape for a published clean rebase.

    The round is zero by definition rather than read: this publication is what
    resets it, so the record of it is about a reviewer with no rounds spent on
    the head it names. Spelled rather than read so the emit can sit ahead of
    the write that resets it -- which is what lets the finish record that it
    announced itself while the anchor is still pinned.
    """
    context.gh.emit_event(
        "base_rebased",
        issue_number=context.issue.number,
        stage=stage_name(context.label),
        pr_number=context.pr_number,
        sha=after_sha,
        method="auto_clean_rebase",
        review_round=0,
        retry_count=context.state.get("retry_count"),
    )


def _finalize_auto_rebase(
    context: _AutoRebaseContext,
    branch: str,
    after_sha: str,
) -> None:
    """Publish the notice, audit event, validating route, and pinned state.

    The pinned write that clears the attempt goes last so a tick that dies
    partway leaves the anchor for the next one to recover from. What the
    announcement owes that ordering is a durable mark of its own, written
    while the anchor still stands: the notice and the audit event are out
    before the relabel, and a tick lost between them and the last write would
    otherwise come back to an attempt that looks unfinished and say all of it
    a second time.
    """
    _post_auto_rebase_notice(context, after_sha)
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
    persistence._announced(context, after_sha)
    persistence._clears_the_attempt(context.state)
    context.state.set(_REVIEW_ROUND, 0)
    context.gh.set_workflow_label(context.issue, WorkflowLabel.VALIDATING)
    context.gh.write_pinned_state(context.issue, context.state)


def _publish_auto_rebase(
    context: _AutoRebaseContext,
    before_sha: str,
) -> None:
    """Validate and force-publish a successfully rebased PR worktree."""
    after_sha = probes._head_sha(context.worktree)
    if not after_sha:
        guards._park_unreadable_post_rebase_head(context, before_sha)
        return
    if after_sha == before_sha:
        guards._finish_noop_auto_rebase(context)
        return

    dirty_files = probes._worktree_dirty_files(context.worktree)
    if dirty_files:
        guards._park_dirty_auto_rebase(context, before_sha, dirty_files)
        return

    branch = paths._resolve_branch_name(
        context.state, context.spec, context.issue.number,
    )
    records = _gate_records()
    published = _gated_publication()._publishes(
        records._gate(
            context.gh, context.spec, context.issue, context.state,
            context.worktree,
        ),
        branch,
        records._Entered(
            head=before_sha or "",
            reconciling=True,
            # The head this refresh read for itself, and the one the notice,
            # the audit event, and the log line below all name. Between that
            # read and the gate's own the worktree is writable, so a commit
            # landing in the window would be pushed and receipted here while
            # the tail went on to finalize the SHA this owner read. Named, the
            # two are one decision and a checkout that moved refuses before
            # anything reaches the remote.
            candidate=after_sha,
            # What this rebase replaced, so a replay of the exact commit an
            # adjudication accepted is recognized as the same contribution
            # rather than measured past the same ceiling and adjudicated a
            # second time with a pull request already open over it.
            rewrite=transfers._rewritten_by_the_rebase(
                context, before_sha, after_sha,
            ),
        ),
    )
    if published.held:
        # The size gate owns the issue from here -- parked, or handed to the
        # adjudication under `workflow:decomposing` -- so the notice, the
        # audit event, and the `validating` route below are not this refresh's
        # to make. The pinned write still is: a park leaves its flags in
        # memory for the caller that took it.
        context.gh.write_pinned_state(context.issue, context.state)
        return
    if not published.landed:
        guards._park_failed_auto_rebase_push(context, before_sha, branch)
        return
    _finalize_auto_rebase(context, branch, after_sha)
