# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Documenting run."""
from __future__ import annotations

from orchestrator.stages import _documenting_state as _state
from orchestrator.stages import documenting as _owner

_DocumentingContext = _owner._DocumentingContext
_DocumentingRun = _owner._DocumentingRun
AgentResult = _owner.AgentResult
config = _owner.config
filter_trusted = _owner.filter_trusted
_AWAITING_HUMAN = _state._AWAITING_HUMAN
_LAST_ACTION_COMMENT_ID = _state._LAST_ACTION_COMMENT_ID


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
        _owner._park_documenting(
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
        _owner._park_documenting(
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
        ctx.gh.comments_after(ctx.issue, ctx.state.get(_LAST_ACTION_COMMENT_ID)),
    )
    if not new_comments:
        return None
    ctx.state.set(
        _LAST_ACTION_COMMENT_ID, max(comment.id for comment in new_comments),
    )
    # Anchor `before_sha` from the just-fetched PR worktree BEFORE the resume
    # so the post-spawn check sees a real difference if (and only if) the
    # resumed dev produced a new commit. Persist `docs_checked_sha` BEFORE the
    # spawn for the same reason the fresh-spawn shape does: a no-change verdict
    # on this resume relies on this watermark to identify the confirmed commit.
    before_sha = _wf._head_sha(wt)
    ctx.state.set("docs_checked_sha", before_sha or "")
    wt, documentation_result, paused = _wf._resume_dev_with_text(
        ctx.gh, ctx.spec, ctx.issue, ctx.state, _owner._documentation_prompt(ctx),
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
        ctx.gh, ctx.spec, ctx.issue, ctx.state, _owner._documentation_prompt(ctx),
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
    if ctx.state.get(_AWAITING_HUMAN):
        return _owner._resume_documenting_dev(ctx, wt, ahead)
    if ahead > 0:
        return _owner._recovered_documenting_run(ctx, wt, ahead)
    return _owner._fresh_documenting_run(ctx, wt, ahead)
