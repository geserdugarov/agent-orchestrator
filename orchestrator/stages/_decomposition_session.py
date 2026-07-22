# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Decomposition session."""
from __future__ import annotations

from orchestrator.stages import _decomposition_state as _state
from orchestrator.stages import decomposition as _owner

_DecomposerSession = _owner._DecomposerSession
AgentResult = _owner.AgentResult
GitHubClient = _owner.GitHubClient
Issue = _owner.Issue
Optional = _owner.Optional
PinnedState = _owner.PinnedState
Tuple = _owner.Tuple
config = _owner.config
filter_trusted = _owner.filter_trusted
_AWAITING_HUMAN = _state._AWAITING_HUMAN
_CHILDREN = _state._CHILDREN
_LAST_ACTION_COMMENT_ID = _state._LAST_ACTION_COMMENT_ID


def _read_decomposer_session(
    state: PinnedState,
) -> Tuple[str, str, tuple[str, ...], Optional[str]]:
    """Return (spec, backend, extra_args, decomposer_session_id) for an issue.

    Mirrors `_read_dev_session`: `spec` is the full configured agent
    command string the next run will use, returned so callers can
    persist it verbatim BEFORE invoking `run_agent` -- a fresh
    decomposer that produces a manifest without surfacing a session id
    (a backend hiccup in the JSONL output, an empty `-o` file) would
    otherwise leave `decomposer_agent` unset and a later
    `DECOMPOSE_AGENT` env flip could retarget the awaiting-human
    resume at a backend that never ran on this issue.

    Legacy bare-backend values (`"codex"` / `"claude"`) re-parse to
    `(backend, ())` and round-trip cleanly. When the issue has never
    been spawned, returns the current config's
    `(DECOMPOSE_AGENT_SPEC, DECOMPOSE_AGENT, DECOMPOSE_AGENT_ARGS, None)`.
    """
    stored = state.get("decomposer_agent")
    if stored:
        spec = str(stored)
        backend, args = config._parse_agent_spec("decomposer_agent", spec)
        sid = state.get("decomposer_session_id")
        return spec, backend, args, None if sid is None else str(sid)
    return (
        config.DECOMPOSE_AGENT_SPEC,
        config.DECOMPOSE_AGENT,
        config.DECOMPOSE_AGENT_ARGS,
        None,
    )


def _resume_decomposer_on_human_reply(
    gh: GitHubClient, spec: config.RepoSpec, issue: Issue, state: PinnedState
) -> Optional[AgentResult]:
    """Resume the decomposer's locked-backend session with new comments.

    Returns the agent result, or None if there are no new comments since
    the last park (caller should return without writing state).

    Mirrors `_resume_developer_on_human_reply` but on the decomposer
    session. The backend is locked to whichever wrote
    `decomposer_session_id`; resuming across backends would need an
    inter-backend session bridge that does not exist.
    """
    from orchestrator import workflow as _wf

    followup = _owner._decomposer_followup(gh, issue, state)
    if followup is None:
        return None
    wt = _wf._decompose_worktree_path(spec, issue.number)
    if not wt.exists():
        wt = _wf._ensure_decompose_worktree(spec, issue.number)
    session = _DecomposerSession(*_owner._read_decomposer_session(state))
    decomposer_result = _wf._run_agent_tracked(
        gh, issue.number,
        agent_role="decomposer",
        stage="decomposing",
        backend=session.backend,
        prompt=followup,
        cwd=wt,
        agent_spec=session.spec,
        resume_session_id=session.session_id,
        extra_args=session.extra_args,
        retry_count=state.get("retry_count"),
    )
    state.set(_AWAITING_HUMAN, False)
    return decomposer_result


def _decomposer_followup(
    gh: GitHubClient, issue: Issue, state: PinnedState,
) -> Optional[str]:
    comments = filter_trusted(
        gh.comments_after(issue, state.get(_LAST_ACTION_COMMENT_ID))
    )
    if not comments:
        return None
    from orchestrator import workflow as _wf

    state.set(_LAST_ACTION_COMMENT_ID, max(comment.id for comment in comments))
    return "\n\n".join(
        _wf._quote_comment_line(comment)
        for comment in comments if comment.body
    )


def _reset_decomposing_on_drift(
    gh: GitHubClient, issue: Issue, state: PinnedState
) -> None:
    """Wipe manifest tracking and the decomposer session when the issue
    body drifted, so the fresh-spawn path re-derives a manifest against
    the updated body THIS tick.

    Runs at the very top of `_handle_decomposing` -- the spec requires
    "at the start of every per-tick handler". Ordering it before the
    half-finished recovery is what stops the recovery branch from
    finalizing to `blocked` / `umbrella` against a stale manifest when the
    human edited the issue body during a crash window. When drift IS
    detected we clear the manifest tracking (children, dep_graph,
    expected_children_count, umbrella) so the recovery branch is bypassed
    and the fresh-spawn path derives a new manifest. Previously-created
    children are listed as orphans in the notice -- they remain on GitHub
    but the orchestrator no longer tracks them.

    Unlike the pre-implementation handlers (which call
    `_route_drift_to_decomposing` and RETURN), this issue is already
    `decomposing`, so we mutate state in place and fall through -- the
    caller keeps running and spawns the decomposer this tick.
    """
    from orchestrator import workflow as _wf

    new_hash = _wf._detect_user_content_change(gh, issue, state)
    if new_hash is None:
        return
    _wf._post_issue_comment(
        gh, issue, state,
        _owner._decomposition_drift_notice(list(state.get(_CHILDREN) or [])),
    )
    state.set("user_content_hash", new_hash)
    # Drop only the SESSION id -- preserve `decomposer_agent`
    # (the locked role spec). Lock-on-first-spawn means a
    # mid-flight `DECOMPOSE_AGENT` env flip must not retarget
    # an in-flight issue at a different backend; the fresh
    # spawn below picks up the recorded spec via
    # `_read_decomposer_session`.
    _owner._clear_decomposition_manifest(state)
