# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Decomposition stage handlers.

Covers the `decomposing` / `ready` / `blocked` / `umbrella` labels and
their stage-private helpers (decomposer session lookup, awaiting-human
resume, half-finished decomposition recovery, child issue creation,
dependency activation, and the DECOMPOSE kill-switch bailout).

`_handle_decomposing` is a thin router over stage-private helpers:
user-content drift reset (`_reset_decomposing_on_drift`), half-finished
recovery / stale manifest cleanup (`_recover_stale_manifest`), the
DECOMPOSE kill-switch bailout (`_route_disabled_to_implementing`), the
fresh decomposer spawn (`_spawn_fresh_decomposer`) or awaiting-human
resume (`_resume_decomposer_on_human_reply`), the post-run settlement
(`_settle_decomposer_run`, folds usage and parks on pause / timeout), and
the manifest-outcome dispatch (`_dispatch_decomposer_manifest`) --
invalid/silent park (`_park_unparsed_manifest`), `single` finalize
(`_finalize_single_decision`), or `split` child creation
(`_create_child_issues`) plus parent finalize + activation
(`_finalize_split`). The read-only dirty-worktree park stays inline in
`_handle_decomposing` so `keep_worktree` is set before its side effects.
`_handle_blocked` and `_handle_umbrella` share the
child-poll helpers (`_route_parent_drift`, `_read_child_labels`,
`_park_rejected_children`, `_park_manually_closed_children`,
`_activate_ready_children`, `_log_held_children`); `_handle_ready` shares
the drift route-back. None of these helpers are re-exported from
`workflow.py`; only the four stage handlers and `_read_decomposer_session`
stay on the compatibility surface.

ALL workflow-owned helpers (`_park_awaiting_human`, `_run_agent_tracked`,
`_now_iso`, `_handle_implementing`, the worktree plumbing, the drift /
manifest / messaging helpers re-exported into `workflow`) are reached
through the parent module via `from .. import workflow as _wf` at call
time. The compatibility surface tests rely on -- `patch.object(workflow,
"_foo")` -- has to keep working from inside the stage module too, so the
handlers must NOT direct-import these names from `workflow_drift` /
`workflow_messages` / `worktrees`; doing so would bind a stable
reference that test patches against `workflow.X` could not affect.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Tuple

from github.Issue import Issue

from orchestrator import config
from orchestrator.agents import AgentResult
from orchestrator.comment_trust import filter_trusted
from orchestrator.config import RepoSpec
from orchestrator.state_machine import WorkflowLabel
from orchestrator.github import GitHubClient, PinnedState


@dataclass(frozen=True)
class _DecomposerRunPlan:
    agent_result: Optional[AgentResult]
    keep_worktree: bool = False


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
        return spec, backend, args, str(sid) if sid is not None else None
    return (
        config.DECOMPOSE_AGENT_SPEC,
        config.DECOMPOSE_AGENT,
        config.DECOMPOSE_AGENT_ARGS,
        None,
    )


def _resume_decomposer_on_human_reply(
    gh: GitHubClient, spec: RepoSpec, issue: Issue, state: PinnedState
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

    last_action_id = state.get("last_action_comment_id")
    # Drop untrusted authors up front (mirrors `_resume_developer_on_human_reply`):
    # with `ALLOWED_ISSUE_AUTHORS` set an outsider reply on a parked decomposer
    # session must not steer it NOR advance the consumed watermark. Only trusted
    # comments are consumed, so an outsider reply trailing a trusted one is left
    # unconsumed; an all-untrusted batch leaves nothing to resume on.
    new_comments = filter_trusted(gh.comments_after(issue, last_action_id))
    if not new_comments:
        return None
    consumed_max = max(comment.id for comment in new_comments)
    state.set("last_action_comment_id", consumed_max)
    followup = "\n\n".join(
        f"@{comment.user.login if comment.user else 'user'}: {comment.body}"
        for comment in new_comments
        if comment.body
    )
    wt = _wf._decompose_worktree_path(spec, issue.number)
    if not wt.exists():
        wt = _wf._ensure_decompose_worktree(spec, issue.number)
    decomposer_spec, decomposer_backend, decomposer_args, decomposer_sid = (
        _read_decomposer_session(state)
    )
    decomposer_result = _wf._run_agent_tracked(
        gh, issue.number,
        agent_role="decomposer",
        stage="decomposing",
        backend=decomposer_backend,
        prompt=followup,
        cwd=wt,
        agent_spec=decomposer_spec,
        resume_session_id=decomposer_sid,
        extra_args=decomposer_args,
        retry_count=state.get("retry_count"),
    )
    state.set("awaiting_human", False)
    return decomposer_result


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
    orphans = list(state.get("children") or [])
    if orphans:
        orphan_list = ", ".join(
            f"#{child_number}" for child_number in orphans
        )
        notice = (
            ":pencil2: issue content changed; re-running "
            "decomposer against the updated body. The "
            f"previously-tracked children ({orphan_list}) "
            "will be ORPHANED -- the orchestrator no longer "
            "tracks them; please close any that no longer "
            "apply to the updated requirements."
        )
    else:
        notice = (
            ":pencil2: issue content changed; re-running "
            "decomposer against the updated body."
        )
    _wf._post_issue_comment(gh, issue, state, notice)
    state.set("user_content_hash", new_hash)
    # Drop only the SESSION id -- preserve `decomposer_agent`
    # (the locked role spec). Lock-on-first-spawn means a
    # mid-flight `DECOMPOSE_AGENT` env flip must not retarget
    # an in-flight issue at a different backend; the fresh
    # spawn below picks up the recorded spec via
    # `_read_decomposer_session`.
    state.set("decomposer_session_id", None)
    state.set("children", [])
    state.set("dep_graph", {})
    state.set("expected_children_count", None)
    state.set("umbrella", None)
    state.set("awaiting_human", False)
    state.set("park_reason", None)


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
    from orchestrator import workflow as _wf

    expected_raw = state.get("expected_children_count")
    children_recorded = state.get("children") or []
    if expected_raw is None and not children_recorded:
        return False
    if state.get("awaiting_human"):
        return True
    if expected_raw is not None and len(children_recorded) < int(expected_raw):
        _wf._park_awaiting_human(
            gh, issue, state,
            f"{config.HITL_MENTIONS} decomposition crashed mid-way: "
            f"{len(children_recorded)} of {expected_raw} children "
            "recorded (an orphan child issue may exist on GitHub if "
            "the crash landed between `create_child_issue` returning "
            "and the parent state write); manual intervention needed "
            "(close any partial children and re-decompose, or finish "
            "creating the missing ones).",
            reason="decomposition_crash",
        )
        gh.write_pinned_state(issue, state)
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
    for child_number in children_recorded:
        try:
            child_issue = gh.get_issue(int(child_number))
            child_state = gh.read_pinned_state(child_issue)
            if not child_state.get("parent_number"):
                child_state.set("parent_number", issue.number)
                if not child_state.get("created_at"):
                    child_state.set("created_at", _wf._now_iso())
                child_state.set("awaiting_human", False)
                child_state.set("park_reason", None)
                gh.write_pinned_state(child_issue, child_state)
        except Exception:
            _wf.log.exception(
                "issue=#%s could not repair orphan child #%s during "
                "decomposition recovery", issue.number, child_number,
            )
            _wf._park_awaiting_human(
                gh, issue, state,
                f"{config.HITL_MENTIONS} could not repair child "
                f"#{child_number} during decomposition recovery "
                "(seed `parent_number` on its pinned state); manual "
                "intervention needed (check orchestrator logs).",
                reason="child_seed_failed",
            )
            gh.write_pinned_state(issue, state)
            return True
    # `umbrella=True` is persisted alongside `expected_children_count`
    # before any child is created, so the recovery path here picks
    # it up and finalizes to `umbrella` instead of `blocked`. Without
    # this branch, a SIGKILL between the umbrella manifest's child
    # creation loop and the final label flip would resume as a
    # plain blocked parent and re-enter implementation after all
    # children resolved -- the opposite of what the manifest asked.
    finalize_label = (
        WorkflowLabel.UMBRELLA if state.get("umbrella")
        else WorkflowLabel.BLOCKED
    )
    gh.set_workflow_label(issue, finalize_label)
    gh.write_pinned_state(issue, state)
    return True


def _route_disabled_to_implementing(
    gh: GitHubClient, spec: RepoSpec, issue: Issue, state: PinnedState
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
    state.set("awaiting_human", False)
    state.set("park_reason", None)
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
        prior = state.get("last_action_comment_id")
        if not isinstance(prior, int) or latest > prior:
            state.set("last_action_comment_id", latest)
    gh.set_workflow_label(issue, WorkflowLabel.IMPLEMENTING)
    gh.write_pinned_state(issue, state)
    _wf._handle_implementing(gh, spec, issue)
    return True


def _spawn_fresh_decomposer(
    gh: GitHubClient, spec: RepoSpec, issue: Issue, state: PinnedState
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
    decomposer_spec, decomposer_backend, decomposer_args, _ = (
        _read_decomposer_session(state)
    )
    # Persist the spec BEFORE the spawn so a backend hiccup
    # that yields no `session_id` -- yet still produces a
    # manifest in the worktree or parks awaiting human -- does
    # not leave `decomposer_agent` unset. A later
    # `DECOMPOSE_AGENT` flip would otherwise retarget the next
    # awaiting-human resume at a backend that never ran on
    # this issue. Storing the parsed backend alone would also
    # strip configured CLI args on subsequent resumes.
    state.set("decomposer_agent", decomposer_spec)
    prompt = _wf._build_decompose_prompt(
        spec, issue, _wf._recent_comments_text(issue),
        config.default_repo_specs(),
    )
    decomposer_result = _wf._run_agent_tracked(
        gh, issue.number,
        agent_role="decomposer",
        stage="decomposing",
        backend=decomposer_backend,
        prompt=prompt,
        cwd=wt,
        agent_spec=decomposer_spec,
        extra_args=decomposer_args,
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
    if error is not None:
        quoted = "> " + last_msg.strip().replace("\n", "\n> ")
        _wf._park_awaiting_human(
            gh, issue, state,
            f"{config.HITL_MENTIONS} decomposer manifest invalid "
            f"({error}); manual adjudication needed.\n\n"
            f"_Last decomposer message:_\n\n{quoted}",
            reason="decomposer_invalid_manifest",
        )
    else:
        stripped = last_msg.strip()
        raw = stripped or "(decomposer produced no final message)"
        quoted = "> " + raw.replace("\n", "\n> ")
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
            reason="decomposer_silent" if not stripped else "decomposer_question",
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


def _create_child_issues(
    gh: GitHubClient, issue: Issue, state: PinnedState,
    children_manifest: list, is_umbrella: bool,
) -> Optional[Tuple[list, dict]]:
    """Crash-safe child issue creation loop for a `split` manifest.

    Returns `(created, dep_graph)` on success, or None when a create/seed
    step failed and the parent was parked (caller must return).

    Crash-safe sequence:
      1. Persist `expected_children_count` (and the umbrella flag) BEFORE
         creating any child. The half-finished recovery uses these to tell
         a partial loop apart from a completed one, and to finalize to the
         right label after a mid-loop SIGKILL.
      2. For each child: create the GitHub issue, then IMMEDIATELY record
         its number in parent state (before any further non-idempotent
         work). A SIGKILL between these two steps is unavoidable; persisting
         first means the worst case is an orphan child without seeded
         `parent_number`, not a duplicate child created by a decomposer
         respawn.
      3. Seed child pinned state. Failure here parks but parent state
         already records the child, so no respawn happens.
    """
    from orchestrator import workflow as _wf

    created: list[Tuple[int, dict]] = []
    dep_graph: dict[str, list[int]] = {}
    state.set("expected_children_count", len(children_manifest))
    # Persist the umbrella flag alongside the count so the half-finished
    # recovery path can finalize to the right label after a mid-loop
    # SIGKILL. Always write it (including when False) so a buggy state
    # migration that left a stale True from a prior aborted decomposition
    # cannot survive into the recovery branch.
    state.set("umbrella", is_umbrella)
    gh.write_pinned_state(issue, state)
    for idx, child in enumerate(children_manifest):
        depends_on = list(child.get("depends_on") or [])
        try:
            new_issue = gh.create_child_issue(
                title=child["title"],
                body=child["body"],
                parent_number=issue.number,
                labels=[WorkflowLabel.BLOCKED],
            )
        except Exception:
            _wf.log.exception(
                "issue=#%s could not create child %d (%r)",
                issue.number, idx, child.get("title"),
            )
            _wf._park_awaiting_human(
                gh, issue, state,
                f"{config.HITL_MENTIONS} could not create child issue "
                f"index={idx} ({child.get('title')!r}); manual intervention "
                "needed (check orchestrator logs).",
                reason="child_create_failed",
            )
            gh.write_pinned_state(issue, state)
            return None

        # Persist the child number on the parent BEFORE doing any
        # further work for this child. A SIGKILL between
        # `create_child_issue` returning and this write would leave
        # an orphan child on GitHub that the parent does not know
        # about; the next tick would re-spawn the decomposer and
        # create duplicates.
        created.append((new_issue.number, child))
        if depends_on:
            dep_graph[str(idx)] = depends_on
        state.set(
            "children",
            [child_number for child_number, _ in created],
        )
        if dep_graph:
            state.set("dep_graph", dep_graph)
        state.set("decomposed_at", _wf._now_iso())
        gh.write_pinned_state(issue, state)

        # Seed `parent_number` on the child. Mandatory: without
        # it `_handle_blocked` parks the child as "manual relabel
        # suspected" and that park leaves `awaiting_human=True`
        # behind even after the parent later flips the child's
        # label to `ready` -- the child's `_handle_implementing`
        # would then sit waiting for a human comment instead of
        # starting work.
        try:
            child_state = PinnedState()
            child_state.set("parent_number", issue.number)
            child_state.set("created_at", _wf._now_iso())
            gh.write_pinned_state(new_issue, child_state)
        except Exception:
            _wf.log.exception(
                "issue=#%s could not seed pinned state on child #%d",
                issue.number, new_issue.number,
            )
            # Parent already records the child (no duplicate
            # risk). Park so a human can either seed the child
            # manually or close it.
            _wf._park_awaiting_human(
                gh, issue, state,
                f"{config.HITL_MENTIONS} created child #{new_issue.number} "
                f"({child.get('title')!r}) but could not seed its pinned "
                "state with `parent_number`; manual intervention needed "
                "(seed parent_number on the child or close it).",
                reason="child_seed_failed",
            )
            gh.write_pinned_state(issue, state)
            return None
    return created, dep_graph


def _finalize_split(
    gh: GitHubClient, issue: Issue, state: PinnedState,
    created: list, dep_graph: dict, is_umbrella: bool,
) -> None:
    """Post the split summary, flip the parent label, and activate children.

    children/dep_graph/decomposed_at are already durable from the
    incremental writes in `_create_child_issues`. Flip the parent label to
    `blocked` (or `umbrella` when the parent has no implementation work of
    its own), then activate no-dep children. Activation only runs AFTER the
    final parent-state write, so a crash here cannot leave a runnable
    orphan child against a `decomposing`-labeled parent.
    """
    from orchestrator import workflow as _wf

    summary = "\n".join(
        f"- #{child_number}: {child['title']}"
        for child_number, child in created
    )
    if is_umbrella:
        summary_intro = (
            f":bookmark_tabs: decomposer split this into {len(created)} "
            f"child issue(s); marking parent as `umbrella` (no "
            f"implementation of its own; will auto-resolve once every "
            f"child resolves):\n\n{summary}"
        )
        final_label = WorkflowLabel.UMBRELLA
    else:
        summary_intro = (
            f":bookmark_tabs: decomposer split this into {len(created)} "
            f"child issue(s):\n\n{summary}"
        )
        final_label = WorkflowLabel.BLOCKED
    _wf._post_issue_comment(gh, issue, state, summary_intro)
    gh.set_workflow_label(issue, final_label)
    gh.write_pinned_state(issue, state)

    # Activation: flip no-dep children from `blocked` to `ready`.
    # Best-effort -- if any flip fails the parent's `_handle_blocked`
    # walk handles it on the next tick (the walk treats a child with
    # no recorded deps as deps-satisfied).
    for idx, (child_number, _) in enumerate(created):
        if str(idx) in dep_graph:
            continue
        try:
            child_issue = gh.get_issue(child_number)
            gh.set_workflow_label(child_issue, WorkflowLabel.READY)
        except Exception:
            _wf.log.exception(
                "issue=#%s could not flip child #%d to ready; the parent's "
                "_handle_blocked walk will retry on the next tick",
                issue.number, child_number,
            )


def _settle_decomposer_run(
    gh: GitHubClient,
    issue: Issue,
    state: PinnedState,
    decomposer_result: AgentResult,
) -> bool:
    """Fold this run's usage and park on a live pause or timeout.

    Returns True when the caller must return (paused or timed out), False
    to continue to the dirty-worktree check and manifest dispatch. None of
    these paths preserve the decompose worktree: the caller's `finally`
    tears it down on return. The read-only dirty/commits park (which DOES
    preserve the worktree) stays inline in `_handle_decomposing` so
    `keep_worktree` is set BEFORE the park's side effects run.
    """
    from orchestrator import workflow as _wf

    # Live pause: an operator applied `paused` / `backlog` while the
    # decomposer ran (fresh spawn or awaiting-human resume). Dispatch only
    # saw the pre-run labels, so re-check a freshly fetched issue and return
    # WITHOUT folding usage, parking on timeout, creating child issues,
    # relabeling, or writing pinned state -- durable GitHub state stays
    # exactly as the prior tick left it and the next tick re-runs the
    # decomposer once the label is removed. The read-only decompose worktree
    # is torn down by the caller's `finally` as on any normal exit and
    # recreated on the re-run.
    if _wf._paused_during_agent_run(gh, issue):
        return True

    state.set("last_agent_action_at", _wf._now_iso())
    # Fold this run's usage into the per-issue counters at the convergence
    # of the fresh-spawn and awaiting-human resume branches, so a real
    # resume exit is counted exactly once and the no-new-comment resume
    # (which returned above without running the agent) never touches the
    # counters. Interrupted runs are excluded entirely: the read-only
    # dirty/commits park below still writes pinned state (to preserve the
    # inspection worktree), so folding a killed run's usage first would
    # persist a counter the interrupted contract says must not accrue. The
    # clean-interrupted case is additionally short-circuited by the
    # `_ignore_if_interrupted` guard in `_handle_decomposing`.
    if not decomposer_result.interrupted:
        _wf._accumulate_issue_usage(state, decomposer_result.usage)

    if decomposer_result.timed_out:
        _wf._park_awaiting_human(
            gh, issue, state,
            f"{config.HITL_MENTIONS} decomposer timed out after "
            f"{config.AGENT_TIMEOUT}s, manual intervention needed.",
            reason="decomposer_timeout",
        )
        gh.write_pinned_state(issue, state)
        return True
    return False


def _dispatch_decomposer_manifest(
    gh: GitHubClient,
    issue: Issue,
    state: PinnedState,
    decomposer_result: AgentResult,
) -> None:
    """Parse the decomposer's final message and route on the outcome.

    Parks awaiting human on an invalid / silent / question manifest,
    finalizes a `single` decision to `ready`, or creates the `split`
    children and finalizes the parent to `blocked` / `umbrella`.
    """
    from orchestrator import workflow as _wf

    last_msg = decomposer_result.last_message or ""
    parsed, error = _wf._parse_manifest(last_msg)

    if parsed is None:
        _park_unparsed_manifest(
            gh, issue, state, decomposer_result, error,
        )
        return

    if parsed["decision"] == "single":
        _finalize_single_decision(gh, issue, state, parsed)
        return

    # decision == "split".
    children_manifest = parsed["children"]
    is_umbrella = bool(parsed.get("umbrella"))
    created_deps = _create_child_issues(
        gh, issue, state, children_manifest, is_umbrella
    )
    if created_deps is None:
        return
    created, dep_graph = created_deps
    _finalize_split(gh, issue, state, created, dep_graph, is_umbrella)


def _prepare_decomposer_run(
    gh: GitHubClient,
    spec: RepoSpec,
    issue: Issue,
    state: PinnedState,
) -> Optional[_DecomposerRunPlan]:
    # User-content drift FIRST, so it runs BEFORE the half-finished recovery:
    # otherwise recovery could finalize against a stale manifest when the issue
    # was edited during a crash window.
    _reset_decomposing_on_drift(gh, issue, state)

    if _recover_stale_manifest(gh, issue, state):
        return None

    if _route_disabled_to_implementing(gh, spec, issue, state):
        return None

    if state.get("awaiting_human"):
        decomposer_result = _resume_decomposer_on_human_reply(
            gh, spec, issue, state,
        )
        if decomposer_result is None:
            # Keep the worktree intact: if a prior tick parked on dirty/commits,
            # the HITL message asks the operator to inspect and reset it before
            # resuming, and cleanup here would silently delete that state.
            return _DecomposerRunPlan(
                agent_result=None,
                keep_worktree=True,
            )
        return _DecomposerRunPlan(agent_result=decomposer_result)

    decomposer_result = _spawn_fresh_decomposer(gh, spec, issue, state)
    if decomposer_result is None:
        return None
    return _DecomposerRunPlan(agent_result=decomposer_result)


def _handle_decomposing(gh: GitHubClient, spec: RepoSpec, issue: Issue) -> None:
    from orchestrator import workflow as _wf

    state = gh.read_pinned_state(issue)

    # Track whether to keep the decomposer worktree past this tick. Set
    # True only in the dirty/commits park below or the awaiting-human
    # no-reply park, where the operator may want to inspect what the agent
    # did. Every other exit (success or park) cleans up via the finally so
    # the next consumer of this issue number starts from current
    # `origin/<base>`.
    keep_worktree = False
    try:
        run_plan = _prepare_decomposer_run(gh, spec, issue, state)
        if run_plan is None:
            return
        keep_worktree = run_plan.keep_worktree
        if run_plan.agent_result is None:
            return
        decomposer_result = run_plan.agent_result

        if _settle_decomposer_run(gh, issue, state, decomposer_result):
            return

        # The decomposer is supposed to be read-only. If it committed or
        # left uncommitted changes, something has gone wrong (prompt
        # ignored, agent misbehaving, operator scratch). Park awaiting
        # human and KEEP the worktree past this tick so the operator can
        # inspect what the decomposer actually produced before resetting.
        # Set `keep_worktree` BEFORE the park's side effects so a failing
        # `_park_awaiting_human` / `write_pinned_state` still leaves the
        # worktree on disk (the finally would otherwise tear it down).
        wt = _wf._decompose_worktree_path(spec, issue.number)
        if _wf._has_new_commits(spec, wt) or _wf._worktree_dirty_files(wt):
            keep_worktree = True
            _wf._park_awaiting_human(
                gh, issue, state,
                f"{config.HITL_MENTIONS} decomposer left commits or "
                "uncommitted changes in the worktree, but it must be "
                "read-only. Reset the worktree before resuming.",
                reason="decomposer_dirty",
            )
            gh.write_pinned_state(issue, state)
            return

        # Shutdown-sweep interruption: a killed decomposer run has no
        # trustworthy manifest. Its empty/partial output would otherwise
        # fall through to the silent/invalid park and persist the session /
        # `last_agent_action_at` mutations. Ignore it and return WITHOUT
        # writing so the next process re-runs from durable state. Placed
        # AFTER the read-only dirty/commits park so an interrupted run that
        # still left changes parks for inspection (preserving the read-only
        # semantics), and BEFORE the manifest parse so no partial
        # `last_message` is read.
        if _wf._ignore_if_interrupted(issue, decomposer_result):
            return

        _dispatch_decomposer_manifest(gh, issue, state, decomposer_result)
    finally:
        if not keep_worktree:
            _wf._cleanup_decompose_worktree(spec, issue.number)


def _route_parent_drift(
    gh: GitHubClient, issue: Issue, state: PinnedState
) -> bool:
    """Route a decomposed parent (or blocked child) back to `decomposing`
    on a user-content edit.

    Returns True when drift was detected and the issue was re-routed
    (caller must return); False when the content is unchanged.

    The hash baseline is initialized by `_detect_user_content_change`
    itself on the first encounter, so a legacy issue still missing the
    field is durably seeded (via the helper's own `write_pinned_state`)
    rather than silently absorbing the next edit as the new baseline. Both
    parent and child cases route to decomposing so the manifest is
    re-derived against the updated body: silently persisting the new
    baseline for a child would let `_handle_ready` later see a matching
    hash and skip the re-decomposer even when the edited body now needs
    splitting. Parents with in-flight children list those children as
    orphans in the notice (the new manifest may overlap; the operator
    closes the obsolete ones manually).
    """
    from orchestrator import workflow as _wf

    new_hash = _wf._detect_user_content_change(gh, issue, state)
    if new_hash is None:
        return False
    orphans = list(state.get("children") or [])
    _wf._route_drift_to_decomposing(gh, issue, state, new_hash, orphans)
    gh.write_pinned_state(issue, state)
    return True


def _read_child_labels(
    gh: GitHubClient, issue: Issue, children: list,
) -> Optional[Tuple[dict, dict]]:
    """Fetch each recorded child issue and its current workflow label.

    Returns `(child_issues, child_labels)` keyed by child number, or None
    if any child read raised (the caller returns and the tick retries on
    the next poll). Labels are read fresh here: the family-aware bucket
    (see `workflow._FAMILY_AWARE_LABELS`) serializes decomposing / blocked
    / umbrella within a tick, so a child's own label flip cannot race this
    read.
    """
    from orchestrator import workflow as _wf

    child_labels: dict[int, Optional[str]] = {}
    child_issues: dict[int, Issue] = {}
    for child_number in children:
        try:
            child_issue = gh.get_issue(int(child_number))
        except Exception:
            _wf.log.exception(
                "issue=#%s could not read child #%d", issue.number, child_number,
            )
            return None
        child_issues[int(child_number)] = child_issue
        child_labels[int(child_number)] = gh.workflow_label(child_issue)
    return child_issues, child_labels


def _park_rejected_children(
    gh: GitHubClient, issue: Issue, state: PinnedState, child_labels: dict,
) -> bool:
    """Park the parent when any child carries the `rejected` label.

    Returns True when parked (caller must return); False otherwise.
    Idempotent by `awaiting_human` so a rejected child does not re-park
    every tick.
    """
    from orchestrator import workflow as _wf

    rejected = [
        child_number
        for child_number, child_label in child_labels.items()
        if child_label == "rejected"
    ]
    if not rejected:
        return False
    if state.get("awaiting_human"):
        return True
    _wf._park_awaiting_human(
        gh, issue, state,
        f"{config.HITL_MENTIONS} child issue(s) rejected: "
        f"{', '.join(f'#{child_number}' for child_number in rejected)}; "
        "decide whether to re-decompose or close.",
        reason="child_rejected",
    )
    gh.write_pinned_state(issue, state)
    return True


def _park_manually_closed_children(
    gh: GitHubClient, spec: RepoSpec, issue: Issue, state: PinnedState,
    child_issues: dict, child_labels: dict,
) -> bool:
    """Park the parent when a child was closed without reaching a terminal
    label.

    Returns True when parked (caller must return); False otherwise. On the
    way, each closed candidate is retried against the PR-merge finalize
    helper and its `child_labels` entry is flipped to `done` if the merge
    finalized -- so an externally-merged child whose label was never
    advanced past an in-flight stage no longer strands the aggregation.

    A child closed manually (e.g. via the GitHub UI) before reaching
    `in_review` is invisible to `list_pollable_issues`, which only sweeps
    closed issues for a small label set (the externally-merged path). Its
    workflow label stays frozen at whatever it was at close, so without
    this branch the parent would read the stale label, neither the rejected
    nor the all-done branch would fire, and the parent would wait forever
    for a child that is gone. `in_review` is intentionally allowed: a
    state=closed/label=in_review child is the externally-merged transient
    that the closed-in_review sweep finalizes on the next tick, NOT a manual
    override.
    """
    from orchestrator import workflow as _wf

    manually_closed = [
        child_number
        for child_number, child_issue in child_issues.items()
        if getattr(child_issue, "state", "open") == "closed"
        and child_labels.get(child_number)
        not in ("done", "rejected", "in_review")
    ]
    if manually_closed:
        still_closed: list[int] = []
        for child_number in manually_closed:
            child_issue = child_issues[child_number]
            child_state = gh.read_pinned_state(child_issue)
            if _wf._finalize_if_pr_merged(gh, spec, child_issue, child_state):
                child_labels[child_number] = "done"
                continue
            still_closed.append(child_number)
        manually_closed = still_closed
    if not manually_closed:
        return False
    if state.get("awaiting_human"):
        return True
    _wf._park_awaiting_human(
        gh, issue, state,
        f"{config.HITL_MENTIONS} child issue(s) closed without reaching "
        f"`done` or `rejected`: "
        f"{', '.join(f'#{child_number}' for child_number in manually_closed)}; "
        "decide whether to re-decompose or close.",
        reason="child_manually_closed",
    )
    gh.write_pinned_state(issue, state)
    return True


def _activate_ready_children(
    gh: GitHubClient, issue: Issue, state: PinnedState,
    children: list, child_labels: dict, child_issues: dict,
) -> list:
    """Dep-graph activation walk shared by `_handle_blocked` / `_handle_umbrella`.

    Any `blocked` child whose recorded dependencies are all `done` gets
    relabeled `ready`. A child with no recorded deps also flips (vacuous
    all-done over an empty list) -- this recovers any no-dep child that the
    decomposer's same-tick activation step left as `blocked` (network blip,
    label-flip failure, etc.). Writes pinned state when at least one child
    was relabeled. Returns the still-held children as
    `[(child_number, pending_dep_numbers)]` for visibility logging.
    """
    dep_graph = state.get("dep_graph") or {}
    relabeled = False
    held: list[tuple[int, list[int]]] = []
    for idx, child_number in enumerate(children):
        cn = int(child_number)
        if child_labels.get(cn) != "blocked":
            continue
        deps = dep_graph.get(str(idx), [])
        dep_numbers = [
            int(children[int(dependency_index)])
            for dependency_index in deps
            if int(dependency_index) < len(children)
        ]
        pending = [dn for dn in dep_numbers if child_labels.get(dn) != "done"]
        if not pending:
            gh.set_workflow_label(child_issues[cn], WorkflowLabel.READY)
            relabeled = True
        else:
            held.append((cn, pending))
    if relabeled:
        gh.write_pinned_state(issue, state)
    return held


def _log_held_children(
    issue: Issue, parent_kind: str, children: list, child_labels: dict,
    held: list,
) -> None:
    """Surface which children are still held under a parent and the exact
    unfinished dependencies gating each, so an operator can see at a glance
    why a decomposed parent is not advancing.

    Children whose deps are satisfied are intentionally NOT held -- they run
    concurrently while the parent waits, which is what drives the tree to
    completion. Logged only when something is held to keep a healthy parent
    from spamming the tick log. `parent_kind` is `"blocked"` or `"umbrella"`.
    """
    from orchestrator import workflow as _wf

    if not held:
        return
    done_count = sum(1 for lbl in child_labels.values() if lbl == "done")
    summary = "; ".join(
        f"#{cn} waits on "
        f"{', '.join(f'#{dependency_number}' for dependency_number in pending)}"
        for cn, pending in held
    )
    _wf.log.info(
        "issue=#%s %s parent: %d/%d children done, %d held: %s",
        issue.number, parent_kind, done_count, len(children), len(held),
        summary,
    )


def _handle_ready(gh: GitHubClient, spec: RepoSpec, issue: Issue) -> None:
    """`ready` is the entry point for an auto-created child or for a parent
    whose decomposer voted `single`. Both cases need the same pickup-state
    seeding the legacy `_handle_pickup` did before flipping to
    `implementing`, so the validating handoff watermark and the in_review
    legacy migration have an anchor comment they can key on.
    """
    from orchestrator import workflow as _wf

    state = gh.read_pinned_state(issue)
    # User-content drift before implementation has started: route back to
    # decomposing so the manifest is re-derived against the new body. A
    # non-umbrella parent can reach `ready` after every child resolves
    # (`_handle_blocked`'s all-done branch flips `blocked` -> `ready`), so
    # the parent may STILL carry `children` / `dep_graph` /
    # `expected_children_count` from the prior manifest. `_route_parent_drift`
    # (via `_route_drift_to_decomposing`) wipes that tracking alongside the
    # locked decomposer session, so the next `_handle_decomposing` tick's
    # half-finished recovery branch does not fire and just flip the issue
    # back to `blocked` without re-running the decomposer.
    if _route_parent_drift(gh, issue, state):
        return
    if state.get("pickup_comment_id") is None:
        if not state.get("created_at"):
            state.set("created_at", _wf._now_iso())
        pickup = _wf._post_issue_comment(
            gh, issue, state,
            ":robot: orchestrator picking this up; starting implementation.",
        )
        pickup_id = getattr(pickup, "id", None)
        if pickup_id is not None:
            state.set("pickup_comment_id", int(pickup_id))
    # Mark every comment visible right now as "already consumed". For a
    # parent that came through `decomposing` / `blocked`, `pickup_comment_id`
    # was anchored on the original "decomposing" comment, so any human
    # feedback posted while children were resolving sits AFTER pickup and
    # would be classified as post-pickup, unconsumed feedback by the
    # in_review watermark seed. The implementer reads the full thread via
    # `_recent_comments_text` at spawn, so by the time the PR reaches
    # `in_review` those comments have been incorporated; replaying them
    # would resume the dev and bounce the PR back to validating instead
    # of allowing merge. Bumping `last_action_comment_id` lets
    # `_seed_watermark_past_self`'s `consumed_through` walk advance past
    # them. The next park (or the validating handoff) will overwrite this
    # value, so it's a transient marker for the in-progress handoff only.
    latest = gh.latest_comment_id(issue)
    if isinstance(latest, int):
        prior = state.get("last_action_comment_id")
        if not isinstance(prior, int) or latest > prior:
            state.set("last_action_comment_id", latest)
    gh.set_workflow_label(issue, WorkflowLabel.IMPLEMENTING)
    gh.write_pinned_state(issue, state)
    _wf._handle_implementing(gh, spec, issue)


def _handle_blocked(gh: GitHubClient, spec: RepoSpec, issue: Issue) -> None:
    """Poll children to decide whether the parent unblocks (or one of the
    children unblocks).

    The orchestrator's parallel tick path (see
    `workflow._FAMILY_AWARE_LABELS`) submits the whole family-aware
    bucket as a single drain task on one worker thread, so only one of
    `decomposing`, `blocked`, or `umbrella` runs at a time within a
    tick -- even when other issues fan out across worker threads. A
    child's `in_review -> done` label flip and this tick therefore
    still cannot race the parent's child-state writes; we read each
    child's current label fresh here. Issues outside the family-aware
    bucket (`implementing`, `validating`, `in_review`,
    `resolving_conflict`) may run concurrently alongside, but their
    handlers do not write across parent/child boundaries.
    """
    from orchestrator import workflow as _wf

    state = gh.read_pinned_state(issue)
    children = state.get("children") or []

    if _route_parent_drift(gh, issue, state):
        return

    if not children:
        # A blocked issue with `parent_number` recorded is a child waiting
        # on a sibling. The parent's `_handle_blocked` walks the dep graph
        # and flips the child to `ready` when its dependencies finish; this
        # tick has nothing to do. Without this branch the polling loop
        # would route every `blocked` child here, treat it as a parent
        # missing its `children` list, and park it as "manual relabel
        # suspected" -- leaving `awaiting_human=True` on the child even
        # after the parent later relabels it `ready`.
        if state.get("parent_number"):
            return
        if state.get("awaiting_human"):
            return
        _wf._park_awaiting_human(
            gh, issue, state,
            f"{config.HITL_MENTIONS} `blocked` without recorded children; "
            "manual relabel suspected.",
            reason="blocked_no_children",
        )
        gh.write_pinned_state(issue, state)
        return

    scan = _read_child_labels(gh, issue, children)
    if scan is None:
        return
    child_issues, child_labels = scan

    if _park_rejected_children(gh, issue, state, child_labels):
        return

    if _park_manually_closed_children(
        gh, spec, issue, state, child_issues, child_labels
    ):
        return

    if all(lbl == "done" for lbl in child_labels.values()):
        _wf._post_issue_comment(
            gh, issue, state,
            ":white_check_mark: all children resolved; ready for "
            "implementation.",
        )
        # Clear any stale park left by a prior `rejected`-child tick: the
        # operator may have re-implemented the rejected child since, and
        # the parent now reaches `ready` legitimately. Without this clear,
        # `awaiting_human=True` survives into `_handle_implementing`,
        # which would route through `_resume_developer_on_human_reply`
        # and either replay long-stale comments or sit silent until a new
        # human reply arrives -- instead of just starting the parent's
        # implementation.
        state.set("awaiting_human", False)
        state.set("park_reason", None)
        gh.set_workflow_label(issue, WorkflowLabel.READY)
        gh.write_pinned_state(issue, state)
        return

    held = _activate_ready_children(
        gh, issue, state, children, child_labels, child_issues
    )
    _log_held_children(issue, "blocked", children, child_labels, held)


def _handle_umbrella(gh: GitHubClient, spec: RepoSpec, issue: Issue) -> None:
    """Poll children on an umbrella parent that has no implementation of
    its own.

    Mirrors `_handle_blocked` for the rejected/manually-closed checks and
    the dep-graph activation walk, but the all-done branch resolves the
    umbrella to `done` and closes the issue instead of flipping it to
    `ready` -- there is no implementation pass for an umbrella, so the
    only terminal path is "every child resolved -> close".
    """
    from orchestrator import workflow as _wf

    state = gh.read_pinned_state(issue)

    # An umbrella parent NEVER enters implementation -- it just closes when
    # every child resolves -- so a body edit cannot be picked up by any
    # later stage's drift check. Route it back to decomposing here so the
    # new manifest is re-derived against the updated body; without this
    # route-back, an edited umbrella would silently close to `done` against
    # the stale manifest once the old children finished.
    if _route_parent_drift(gh, issue, state):
        return

    children = state.get("children") or []
    if not children:
        # An umbrella with no recorded children is corrupt state (the
        # decomposer only applies the umbrella label after creating
        # children), but still surface to a human rather than silently
        # closing an issue with no aggregated work.
        if state.get("awaiting_human"):
            return
        _wf._park_awaiting_human(
            gh, issue, state,
            f"{config.HITL_MENTIONS} `umbrella` without recorded children; "
            "manual relabel suspected.",
            reason="umbrella_no_children",
        )
        gh.write_pinned_state(issue, state)
        return

    scan = _read_child_labels(gh, issue, children)
    if scan is None:
        return
    child_issues, child_labels = scan

    if _park_rejected_children(gh, issue, state, child_labels):
        return

    if _park_manually_closed_children(
        gh, spec, issue, state, child_issues, child_labels
    ):
        return

    if all(lbl == "done" for lbl in child_labels.values()):
        close_body = (
            ":white_check_mark: all children resolved; closing umbrella issue."
        )
        verdict = _wf._format_issue_usage_verdict(state)
        if verdict:
            close_body = f"{close_body}\n\n{verdict}"
        _wf._post_issue_comment(gh, issue, state, close_body)
        state.set("awaiting_human", False)
        state.set("park_reason", None)
        state.set("umbrella_resolved_at", _wf._now_iso())
        gh.set_workflow_label(issue, WorkflowLabel.DONE)
        gh.write_pinned_state(issue, state)
        try:
            issue.edit(state="closed")
        except Exception:
            _wf.log.exception(
                "issue=#%s could not close umbrella after children done",
                issue.number,
            )
        return

    held = _activate_ready_children(
        gh, issue, state, children, child_labels, child_issues
    )
    _log_held_children(issue, "umbrella", children, child_labels, held)
