# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Documenting persistence."""
from __future__ import annotations

from orchestrator.stages import documenting as _owner

_DocumentingContext = _owner._DocumentingContext
PinnedState = _owner.PinnedState
config = _owner.config


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
        _owner._park_documenting(
            ctx,
            f"{config.HITL_MENTIONS} git push failed; see "
            "orchestrator logs.",
            "push_failed",
        )
        return
    _owner._stamp_docs_verdict(ctx.state, after_sha, "updated")
    _owner._post_docs_notice(ctx, notice)
    _owner._advance_after_docs_push(ctx.gh, ctx.issue, ctx.state)
    ctx.gh.write_pinned_state(ctx.issue, ctx.state)


def _documenting_no_change_note(body: str) -> str:
    """Build the `DOCS: NO_CHANGE` PR notice, quoting the agent's
    justification when it supplied one."""
    from orchestrator import workflow as _wf

    justification = body.strip()
    base = ":books: documenting pass: no docs changes required."
    if not justification:
        return base
    quoted = _wf._as_blockquote(justification)
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
        _owner._push_docs_and_advance(
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
    _owner._stamp_docs_verdict(ctx.state, after_sha, "no_change")
    _owner._post_docs_notice(ctx, _owner._documenting_no_change_note(body))
    _owner._advance_after_docs_no_change(ctx.gh, ctx.issue, ctx.state)
    ctx.gh.write_pinned_state(ctx.issue, ctx.state)


def _documenting_commit_notice(recovered: bool) -> str:
    """The `:books:` push notice, distinguishing a recovered commit from a
    fresh docs commit."""
    if recovered:
        return ":books: documenting pass: pushed recovered docs commit(s)."
    return ":books: documenting pass: pushed docs commit."
