# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Decomposition recovery."""
from __future__ import annotations

from orchestrator.stages import _decomposition_state as _state
from orchestrator.stages import decomposition as _owner

_DecomposerSession = _owner._DecomposerSession
AgentResult = _owner.AgentResult
GitHubClient = _owner.GitHubClient
Issue = _owner.Issue
Optional = _owner.Optional
PinnedState = _owner.PinnedState
WorkflowLabel = _owner.WorkflowLabel
config = _owner.config
_AWAITING_HUMAN = _state._AWAITING_HUMAN
_CHILDREN = _state._CHILDREN
_LAST_ACTION_COMMENT_ID = _state._LAST_ACTION_COMMENT_ID
_PARK_REASON = _state._PARK_REASON
_UMBRELLA = _state._UMBRELLA


def _recover_stale_manifest(
    gh: GitHubClient, issue: Issue, state: PinnedState
) -> bool:
    """Half-finished decomposition recovery / stale manifest cleanup.

    Returns True when a recovery path took over and the caller must
    return; False when no manifest markers are present and the caller
    should proceed to spawn the decomposer.

    Two persistent markers signal a prior tick crashed mid-split:
      * `expected_children_count` is written BEFORE any child is created,
        so a SIGKILL after `create_child_issue` returns but before the
        parent records the new child number leaves the parent with this
        marker AND zero recorded children while an orphan child issue
        exists on GitHub. Re-running the decomposer here would emit a
        different manifest and create duplicate children alongside the
        orphan.
      * `children` is written incrementally after each successful create +
        parent-state flush. Its presence covers a crash after at least one
        child was recorded.
    Either marker present without the parent label having flipped to
    `blocked` means we cannot safely respawn the decomposer. Branch by
    whether the recorded count matches expectations: equal -> finalize to
    `blocked`; less -> park awaiting human. Legacy state from a deploy that
    pre-dates `expected_children_count` still routes through the
    `children`-only branch and finalizes.
    """
    expected_raw = state.get("expected_children_count")
    children_recorded = state.get(_CHILDREN) or []
    if expected_raw is None and not children_recorded:
        return False
    if state.get(_AWAITING_HUMAN):
        return True
    if expected_raw is not None and len(children_recorded) < int(expected_raw):
        _owner._park_incomplete_decomposition(
            gh, issue, state, expected_raw, children_recorded,
        )
        return True
    # Before finalizing to `blocked`, repair any child whose pinned
    # state was never seeded. A SIGKILL between the parent's
    # incremental `children` write and the child-state write at
    # the LAST child satisfies `len(children) == expected_children_count`
    # but leaves that child orphaned: no `parent_number`, and likely
    # already parked with `awaiting_human=True` by a prior
    # `_handle_blocked` tick that saw it as "unattributed blocked".
    # Without repair, the parent's later walk flips the orphan to
    # `ready`, but `_handle_implementing` reads the stale park and
    # sits waiting for a human reply that never comes.
    if not _owner._repair_recovered_children(gh, issue, state, children_recorded):
        return True
    # `umbrella=True` is persisted alongside `expected_children_count`
    # before any child is created, so the recovery path here picks
    # it up and finalizes to `umbrella` instead of `blocked`. Without
    # this branch, a SIGKILL between the umbrella manifest's child
    # creation loop and the final label flip would resume as a
    # plain blocked parent and re-enter implementation after all
    # children resolved -- the opposite of what the manifest asked.
    finalize_label = (
        WorkflowLabel.UMBRELLA if state.get(_UMBRELLA)
        else WorkflowLabel.BLOCKED
    )
    gh.set_workflow_label(issue, finalize_label)
    gh.write_pinned_state(issue, state)
    return True


def _route_disabled_to_implementing(
    gh: GitHubClient, spec: config.RepoSpec, issue: Issue, state: PinnedState
) -> bool:
    """DECOMPOSE kill-switch bailout.

    Returns True when decomposition is disabled and the issue was routed
    to implementation (caller must return); False when decomposition is
    enabled and the caller should proceed to spawn the decomposer.

    Every path after this point spawns the decomposer (fresh or via the
    awaiting_human resume), so an operator who restarts with DECOMPOSE=off
    after `_handle_pickup` already labeled the issue `decomposing` -- or
    while it is parked there awaiting a human -- would still see the
    disabled rollout create manifests and child issues. Drop into the
    legacy implementing flow exactly as `_handle_pickup` does on a freshly
    unlabeled issue. The half-finished recovery above must keep running
    regardless of the flag: abandoning orphan children (already on GitHub)
    because new decompositions are now disabled would strand work, which
    is not what a kill switch should do.
    """
    from orchestrator import workflow as _wf

    if config.DECOMPOSE:
        return False
    _wf._post_issue_comment(
        gh, issue, state,
        ":robot: decomposition is disabled; routing this issue "
        "to implementation.",
    )
    # Clear decomposer-side park state. Without this,
    # `_handle_implementing` reads `awaiting_human=True` and
    # tries to resume a dev session that was never spawned --
    # at best it stalls on `comments_after`, at worst the
    # follow-up text becomes the sole prompt instead of the
    # real implement prompt.
    state.set(_AWAITING_HUMAN, False)
    state.set(_PARK_REASON, None)
    # Mark every comment visible at this transition as
    # "already consumed", mirroring `_handle_ready`'s ratchet.
    # `_handle_implementing` will read the full issue thread
    # via `_recent_comments_text` when it builds the implement
    # prompt, so the dev sees any decomposing-era human
    # feedback at spawn. Without this bump, the
    # validating->in_review watermark seed later sees those
    # same comments as fresh PR feedback (because they sit
    # AFTER the now-stale `last_action_comment_id` from the
    # decomposer-era park) and bounces the dev unnecessarily.
    # One-way ratchet so we never lower a higher prior value.
    latest = gh.latest_comment_id(issue)
    if isinstance(latest, int):
        prior = state.get(_LAST_ACTION_COMMENT_ID)
        if not isinstance(prior, int) or latest > prior:
            state.set(_LAST_ACTION_COMMENT_ID, latest)
    gh.set_workflow_label(issue, WorkflowLabel.IMPLEMENTING)
    gh.write_pinned_state(issue, state)
    _wf._handle_implementing(gh, spec, issue)
    return True


def _spawn_fresh_decomposer(
    gh: GitHubClient, spec: config.RepoSpec, issue: Issue, state: PinnedState
) -> Optional[AgentResult]:
    """Consume a retry slot and spawn a fresh decomposer session.

    Returns the agent result, or None when the retry budget is exhausted
    (the budget helper already wrote the park; caller must return).
    """
    from orchestrator import workflow as _wf

    if not _wf._check_and_increment_retry_budget(
        gh, issue, state, stage="decomposing"
    ):
        gh.write_pinned_state(issue, state)
        return None
    wt = _wf._ensure_decompose_worktree(spec, issue.number)
    session = _DecomposerSession(*_owner._read_decomposer_session(state))
    # Persist the spec BEFORE the spawn so a backend hiccup
    # that yields no `session_id` -- yet still produces a
    # manifest in the worktree or parks awaiting human -- does
    # not leave `decomposer_agent` unset. A later
    # `DECOMPOSE_AGENT` flip would otherwise retarget the next
    # awaiting-human resume at a backend that never ran on
    # this issue. Storing the parsed backend alone would also
    # strip configured CLI args on subsequent resumes.
    state.set("decomposer_agent", session.spec)
    decomposer_result = _wf._run_agent_tracked(
        gh, issue.number,
        agent_role="decomposer",
        stage="decomposing",
        backend=session.backend,
        prompt=_wf._build_decompose_prompt(
            spec, issue, _wf._recent_comments_text(issue),
            config.default_repo_specs(),
        ),
        cwd=wt,
        agent_spec=session.spec,
        extra_args=session.extra_args,
        retry_count=state.get("retry_count"),
    )
    if decomposer_result.session_id:
        state.set("decomposer_session_id", decomposer_result.session_id)
    return decomposer_result


def _park_unparsed_manifest(
    gh: GitHubClient, issue: Issue, state: PinnedState,
    decomposer_result: AgentResult, error: Optional[str],
) -> None:
    """Park awaiting human when the decomposer produced no usable manifest.

    Either a malformed manifest (`error` set) OR no manifest at all
    (question / silence, `error` None). Both park; the resume on the next
    comment runs through the awaiting_human branch of `_handle_decomposing`.
    """
    from orchestrator import workflow as _wf

    last_msg = decomposer_result.last_message or ""
    if error is None:
        stripped = last_msg.strip()
        raw = stripped or "(decomposer produced no final message)"
        quoted = _wf._as_blockquote(raw)
        # Only attach stderr diagnostics on the silent path -- a
        # real content question from the decomposer doesn't need
        # the operator wading through subprocess noise.
        diag = (
            "" if stripped
            else _wf._format_stderr_diagnostics(
                decomposer_result, "Decomposer",
            )
        )
        _wf._park_awaiting_human(
            gh, issue, state,
            f"{config.HITL_MENTIONS} decomposer needs your input to "
            f"proceed:\n\n{quoted}{diag}",
            reason="decomposer_question" if stripped else "decomposer_silent",
        )
        if not stripped:
            _wf.log.warning(
                "issue=#%s decomposer produced no final message; "
                "exit_code=%d timed_out=%s stderr_tail=%r",
                issue.number,
                decomposer_result.exit_code,
                decomposer_result.timed_out,
                _wf._stderr_log_tail(decomposer_result),
            )
    else:
        quoted = _wf._as_blockquote(last_msg.strip())
        _wf._park_awaiting_human(
            gh, issue, state,
            f"{config.HITL_MENTIONS} decomposer manifest invalid "
            f"({error}); manual adjudication needed.\n\n"
            f"_Last decomposer message:_\n\n{quoted}",
            reason="decomposer_invalid_manifest",
        )
    gh.write_pinned_state(issue, state)


def _finalize_single_decision(
    gh: GitHubClient, issue: Issue, state: PinnedState, parsed: dict,
) -> None:
    """Finalize a `single` manifest: post the rationale and flip to `ready`.

    Surface the decomposer's rationale AND the context it already gathered
    (affected files, implementation notes) so the develop agent that picks
    this up in `implementing` starts from that groundwork instead of
    re-deriving it. The builder tolerates missing / malformed optional
    fields -- the single decision is already valid, so no cosmetic field
    should park it.
    """
    from orchestrator import workflow as _wf

    _wf._post_issue_comment(
        gh, issue, state,
        _wf._build_single_decision_comment(parsed),
    )
    state.set("decomposed_at", _wf._now_iso())
    gh.set_workflow_label(issue, WorkflowLabel.READY)
    gh.write_pinned_state(issue, state)
