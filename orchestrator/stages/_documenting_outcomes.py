# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Documenting outcomes."""
from __future__ import annotations

from orchestrator.stages import documenting as _owner

_DocumentingContext = _owner._DocumentingContext
_DocumentingRun = _owner._DocumentingRun
AgentResult = _owner.AgentResult
config = _owner.config


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
        _owner._route_documenting_no_change(ctx, wt, ahead, after_sha, body)
        return
    _owner._park_documenting_question(ctx, documentation_result)


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
        _owner._park_documenting(
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
        _owner._park_documenting_dirty(ctx, run.agent_result, dirty)
        return

    if after_sha and after_sha != run.before_sha:
        _owner._push_docs_and_advance(
            ctx, wt, after_sha, _owner._documenting_commit_notice(run.recovered),
        )
        return

    _owner._dispose_documenting_clean(ctx, wt, run.ahead, after_sha, run.agent_result)
