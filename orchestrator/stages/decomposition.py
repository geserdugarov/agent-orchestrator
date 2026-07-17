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
(`_process_decomposer_run`, including usage, pause / timeout handling, and
the read-only worktree guard), and the manifest-outcome dispatch
(`_dispatch_decomposer_manifest`) -- invalid/silent park
(`_park_unparsed_manifest`), `single` finalize
(`_finalize_single_decision`), or `split` child creation
(`_create_child_issues`) plus parent finalize + activation
(`_finalize_split`).
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
from orchestrator.github import (
    QUICK_RUN_LABEL,
    GitHubClient,
    PinnedState,
    issue_has_label,
)


# Pinned-state keys and child-manifest values this stage reads and writes.
_AWAITING_HUMAN = "awaiting_human"
_LAST_ACTION_COMMENT_ID = "last_action_comment_id"
_CHILDREN = "children"
_UMBRELLA = "umbrella"
_PARK_REASON = "park_reason"
_PARENT_NUMBER = "parent_number"
_CREATED_AT = "created_at"
_DONE = "done"


@dataclass
class _DecomposerRunPlan:
    agent_result: Optional[AgentResult]
    keep_worktree: bool = False


@dataclass(frozen=True)
class _DecomposerSession:
    spec: str
    backend: str
    extra_args: tuple[str, ...]
    session_id: Optional[str]


@dataclass
class _SplitPlan:
    children_manifest: list
    is_umbrella: bool
    created: list[Tuple[int, dict]]
    dep_graph: dict[str, list[int]]

    @classmethod
    def start(cls, children_manifest: list, is_umbrella: bool) -> _SplitPlan:
        return cls(children_manifest, is_umbrella, [], {})

    def record(self, idx: int, issue_number: int, child: dict) -> None:
        self.created.append((issue_number, child))
        depends_on = list(child.get("depends_on") or [])
        if depends_on:
            self.dep_graph[str(idx)] = depends_on


@dataclass(frozen=True)
class _ChildScan:
    children: list
    issues: dict[int, Issue]
    labels: dict[int, Optional[str]]


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

    followup = _decomposer_followup(gh, issue, state)
    if followup is None:
        return None
    wt = _wf._decompose_worktree_path(spec, issue.number)
    if not wt.exists():
        wt = _wf._ensure_decompose_worktree(spec, issue.number)
    session = _DecomposerSession(*_read_decomposer_session(state))
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
    state.set(_LAST_ACTION_COMMENT_ID, max(comment.id for comment in comments))
    return "\n\n".join(
        f"@{comment.user.login if comment.user else 'user'}: {comment.body}"
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
        _decomposition_drift_notice(list(state.get(_CHILDREN) or [])),
    )
    state.set("user_content_hash", new_hash)
    # Drop only the SESSION id -- preserve `decomposer_agent`
    # (the locked role spec). Lock-on-first-spawn means a
    # mid-flight `DECOMPOSE_AGENT` env flip must not retarget
    # an in-flight issue at a different backend; the fresh
    # spawn below picks up the recorded spec via
    # `_read_decomposer_session`.
    _clear_decomposition_manifest(state)


def _decomposition_drift_notice(orphans: list) -> str:
    notice = (
        ":pencil2: issue content changed; re-running decomposer against "
        "the updated body."
    )
    if not orphans:
        return notice
    orphan_list = ", ".join(f"#{number}" for number in orphans)
    return (
        f"{notice} The previously-tracked children ({orphan_list}) will be "
        "ORPHANED -- the orchestrator no longer tracks them; please close "
        "any that no longer apply to the updated requirements."
    )


def _clear_decomposition_manifest(state: PinnedState) -> None:
    state.set("decomposer_session_id", None)
    state.set(_CHILDREN, [])
    state.set("dep_graph", {})
    state.set("expected_children_count", None)
    state.set(_UMBRELLA, None)
    state.set(_AWAITING_HUMAN, False)
    state.set(_PARK_REASON, None)


def _park_incomplete_decomposition(
    gh: GitHubClient,
    issue: Issue,
    state: PinnedState,
    expected,
    children: list,
) -> None:
    from orchestrator import workflow as _wf

    _wf._park_awaiting_human(
        gh, issue, state,
        f"{config.HITL_MENTIONS} decomposition crashed mid-way: "
        f"{len(children)} of {expected} children recorded (an orphan child "
        "issue may exist on GitHub if the crash landed between "
        "`create_child_issue` returning and the parent state write); manual "
        "intervention needed (close any partial children and re-decompose, "
        "or finish creating the missing ones).",
        reason="decomposition_crash",
    )
    gh.write_pinned_state(issue, state)


def _repair_recovered_child(
    gh: GitHubClient, issue: Issue, state: PinnedState, child_number,
) -> bool:
    from orchestrator import workflow as _wf

    try:
        child_issue = gh.get_issue(int(child_number))
        child_state = gh.read_pinned_state(child_issue)
        if not child_state.get(_PARENT_NUMBER):
            child_state.set(_PARENT_NUMBER, issue.number)
            if not child_state.get(_CREATED_AT):
                child_state.set(_CREATED_AT, _wf._now_iso())
            child_state.set(_AWAITING_HUMAN, False)
            child_state.set(_PARK_REASON, None)
            gh.write_pinned_state(child_issue, child_state)
    except Exception:
        _wf.log.exception(
            "issue=#%s could not repair orphan child #%s during "
            "decomposition recovery", issue.number, child_number,
        )
        _wf._park_awaiting_human(
            gh, issue, state,
            f"{config.HITL_MENTIONS} could not repair child #{child_number} "
            "during decomposition recovery (seed `parent_number` on its "
            "pinned state); manual intervention needed (check orchestrator "
            "logs).",
            reason="child_seed_failed",
        )
        gh.write_pinned_state(issue, state)
        return False
    return True


def _repair_recovered_children(
    gh: GitHubClient, issue: Issue, state: PinnedState, children: list,
) -> bool:
    return all(
        _repair_recovered_child(gh, issue, state, child_number)
        for child_number in children
    )


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
        _park_incomplete_decomposition(
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
    if not _repair_recovered_children(gh, issue, state, children_recorded):
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
    session = _DecomposerSession(*_read_decomposer_session(state))
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


def _prepare_split_plan(
    gh: GitHubClient, issue: Issue, state: PinnedState, plan: _SplitPlan,
) -> None:
    state.set("expected_children_count", len(plan.children_manifest))
    state.set(_UMBRELLA, plan.is_umbrella)
    gh.write_pinned_state(issue, state)


def _park_child_create_failure(
    gh: GitHubClient,
    issue: Issue,
    state: PinnedState,
    idx: int,
    child: dict,
) -> None:
    from orchestrator import workflow as _wf

    _wf.log.exception(
        "issue=#%s could not create child %d (%r)",
        issue.number, idx, child.get("title"),
    )
    _wf._park_awaiting_human(
        gh, issue, state,
        f"{config.HITL_MENTIONS} could not create child issue index={idx} "
        f"({child.get('title')!r}); manual intervention needed (check "
        "orchestrator logs).",
        reason="child_create_failed",
    )
    gh.write_pinned_state(issue, state)


def _persist_created_child(
    gh: GitHubClient, issue: Issue, state: PinnedState, plan: _SplitPlan,
) -> None:
    from orchestrator import workflow as _wf

    state.set(_CHILDREN, [number for number, _ in plan.created])
    if plan.dep_graph:
        state.set("dep_graph", plan.dep_graph)
    state.set("decomposed_at", _wf._now_iso())
    gh.write_pinned_state(issue, state)


def _seed_created_child(
    gh: GitHubClient,
    issue: Issue,
    state: PinnedState,
    new_issue: Issue,
    child: dict,
) -> bool:
    from orchestrator import workflow as _wf

    try:
        child_state = PinnedState()
        child_state.set(_PARENT_NUMBER, issue.number)
        child_state.set(_CREATED_AT, _wf._now_iso())
        gh.write_pinned_state(new_issue, child_state)
    except Exception:
        _wf.log.exception(
            "issue=#%s could not seed pinned state on child #%d",
            issue.number, new_issue.number,
        )
        _wf._park_awaiting_human(
            gh, issue, state,
            f"{config.HITL_MENTIONS} created child #{new_issue.number} "
            f"({child.get('title')!r}) but could not seed its pinned state "
            "with `parent_number`; manual intervention needed (seed "
            "parent_number on the child or close it).",
            reason="child_seed_failed",
        )
        gh.write_pinned_state(issue, state)
        return False
    return True


def _child_initial_labels(issue: Issue) -> list[str]:
    """Labels every split child is born with.

    Always the initial `blocked` workflow label; plus `quick_run` when the
    parent carries it, so a split parent's accelerated-run modifier propagates
    to the whole child subtree at creation (in the single `create_child_issue`
    write, atomically with `blocked`) rather than being lost at the split
    boundary. Later workflow relabels preserve it -- `set_workflow_label` swaps
    only the workflow label and keeps every control label.
    """
    labels: list[str] = [WorkflowLabel.BLOCKED]
    if issue_has_label(issue, QUICK_RUN_LABEL):
        labels.append(QUICK_RUN_LABEL)
    return labels


def _create_planned_child(
    gh: GitHubClient,
    issue: Issue,
    state: PinnedState,
    plan: _SplitPlan,
    idx: int,
) -> bool:
    child = plan.children_manifest[idx]
    try:
        new_issue = gh.create_child_issue(
            title=child["title"],
            body=child["body"],
            parent_number=issue.number,
            labels=_child_initial_labels(issue),
        )
    except Exception:
        _park_child_create_failure(gh, issue, state, idx, child)
        return False
    plan.record(idx, new_issue.number, child)
    _persist_created_child(gh, issue, state, plan)
    return _seed_created_child(gh, issue, state, new_issue, child)


def _create_child_issues(
    gh: GitHubClient, issue: Issue, state: PinnedState,
    children_manifest: list, is_umbrella: bool,
) -> Optional[_SplitPlan]:
    """Crash-safe child issue creation loop for a `split` manifest.

    Returns the populated split plan on success, or None when a create/seed
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
    plan = _SplitPlan.start(children_manifest, is_umbrella)
    _prepare_split_plan(gh, issue, state, plan)
    for idx in range(len(children_manifest)):
        if not _create_planned_child(gh, issue, state, plan, idx):
            return None
    return plan


def _finalize_split(
    gh: GitHubClient, issue: Issue, state: PinnedState, plan: _SplitPlan,
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

    summary_intro, final_label = _split_summary(plan)
    _wf._post_issue_comment(gh, issue, state, summary_intro)
    gh.set_workflow_label(issue, final_label)
    gh.write_pinned_state(issue, state)
    _activate_initial_split_children(gh, issue, plan)


def _split_summary(plan: _SplitPlan) -> tuple[str, WorkflowLabel]:
    summary = "\n".join(
        f"- #{number}: {child['title']}" for number, child in plan.created
    )
    if plan.is_umbrella:
        return (
            f":bookmark_tabs: decomposer split this into {len(plan.created)} "
            "child issue(s); marking parent as `umbrella` (no implementation "
            "of its own; will auto-resolve once every child resolves):\n\n"
            f"{summary}",
            WorkflowLabel.UMBRELLA,
        )
    return (
        f":bookmark_tabs: decomposer split this into {len(plan.created)} "
        f"child issue(s):\n\n{summary}",
        WorkflowLabel.BLOCKED,
    )


def _activate_initial_split_children(
    gh: GitHubClient, issue: Issue, plan: _SplitPlan,
) -> None:
    from orchestrator import workflow as _wf

    # Activation: flip no-dep children from `blocked` to `ready`.
    # Best-effort -- if any flip fails the parent's `_handle_blocked`
    # walk handles it on the next tick (the walk treats a child with
    # no recorded deps as deps-satisfied).
    for idx, (child_number, _) in enumerate(plan.created):
        if str(idx) in plan.dep_graph:
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
    split_plan = _create_child_issues(
        gh,
        issue,
        state,
        parsed[_CHILDREN],
        bool(parsed.get(_UMBRELLA)),
    )
    if split_plan is None:
        return
    _finalize_split(gh, issue, state, split_plan)


def _prepare_decomposer_run(
    gh: GitHubClient,
    spec: RepoSpec,
    issue: Issue,
    state: PinnedState,
) -> _DecomposerRunPlan:
    # User-content drift FIRST, so it runs BEFORE the half-finished recovery:
    # otherwise recovery could finalize against a stale manifest when the issue
    # was edited during a crash window.
    _reset_decomposing_on_drift(gh, issue, state)

    if _recover_stale_manifest(gh, issue, state):
        return _DecomposerRunPlan(agent_result=None)

    if _route_disabled_to_implementing(gh, spec, issue, state):
        return _DecomposerRunPlan(agent_result=None)

    if state.get(_AWAITING_HUMAN):
        decomposer_result = _resume_decomposer_on_human_reply(
            gh, spec, issue, state,
        )
        return _DecomposerRunPlan(
            agent_result=decomposer_result,
            # A no-reply dirty park keeps its inspection worktree intact.
            keep_worktree=decomposer_result is None,
        )
    return _DecomposerRunPlan(
        agent_result=_spawn_fresh_decomposer(gh, spec, issue, state),
    )


def _process_decomposer_run(
    gh: GitHubClient,
    spec: RepoSpec,
    issue: Issue,
    state: PinnedState,
    run_plan: _DecomposerRunPlan,
) -> None:
    from orchestrator import workflow as _wf

    decomposer_result = run_plan.agent_result
    if decomposer_result is None:
        return

    if _settle_decomposer_run(gh, issue, state, decomposer_result):
        return

    # The decomposer is read-only. Preserve a changed worktree for operator
    # inspection, setting the cleanup policy before parking or persistence can
    # raise and trigger the handler's finally block.
    wt = _wf._decompose_worktree_path(spec, issue.number)
    if _wf._has_new_commits(spec, wt) or _wf._worktree_dirty_files(wt):
        run_plan.keep_worktree = True
        _wf._park_awaiting_human(
            gh, issue, state,
            f"{config.HITL_MENTIONS} decomposer left commits or "
            "uncommitted changes in the worktree, but it must be "
            "read-only. Reset the worktree before resuming.",
            reason="decomposer_dirty",
        )
        gh.write_pinned_state(issue, state)
        return

    # An interrupted run has no trustworthy manifest. The read-only check
    # stays first so changes left by a killed run remain available to inspect.
    if _wf._ignore_if_interrupted(issue, decomposer_result):
        return

    _dispatch_decomposer_manifest(gh, issue, state, decomposer_result)


def _handle_decomposing(gh: GitHubClient, spec: RepoSpec, issue: Issue) -> None:
    from orchestrator import workflow as _wf

    state = gh.read_pinned_state(issue)
    run_plan = _DecomposerRunPlan(agent_result=None)
    try:
        run_plan = _prepare_decomposer_run(gh, spec, issue, state)
        _process_decomposer_run(gh, spec, issue, state, run_plan)
    finally:
        if not run_plan.keep_worktree:
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
    orphans = list(state.get(_CHILDREN) or [])
    _wf._route_drift_to_decomposing(gh, issue, state, new_hash, orphans)
    gh.write_pinned_state(issue, state)
    return True


def _read_child_labels(
    gh: GitHubClient, issue: Issue, children: list,
) -> Optional[_ChildScan]:
    """Fetch each recorded child issue and its current workflow label.

    Returns a child scan with issues and labels keyed by child number, or
    None if any child read raised (the caller returns and the tick retries
    on the next poll). Labels are read fresh here: the family-aware bucket
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
    return _ChildScan(children, child_issues, child_labels)


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
    if state.get(_AWAITING_HUMAN):
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
    scan: _ChildScan,
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

    manually_closed = _manually_closed_children(scan)
    if manually_closed:
        manually_closed = _remaining_manually_closed(
            gh, spec, scan, manually_closed,
        )
    if not manually_closed:
        return False
    if state.get(_AWAITING_HUMAN):
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


def _manually_closed_children(scan: _ChildScan) -> list[int]:
    return [
        number for number, child_issue in scan.issues.items()
        if getattr(child_issue, "state", "open") == "closed"
        and scan.labels.get(number) not in (_DONE, "rejected", "in_review")
    ]


def _remaining_manually_closed(
    gh: GitHubClient,
    spec: RepoSpec,
    scan: _ChildScan,
    candidates: list[int],
) -> list[int]:
    from orchestrator import workflow as _wf

    remaining: list[int] = []
    for number in candidates:
        child_issue = scan.issues[number]
        child_state = gh.read_pinned_state(child_issue)
        if _wf._finalize_if_pr_merged(gh, spec, child_issue, child_state):
            scan.labels[number] = _DONE
        else:
            remaining.append(number)
    return remaining


@dataclass
class _ChildActivation:
    gh: GitHubClient
    state: PinnedState
    scan: _ChildScan
    held: list[tuple[int, list[int]]]
    relabeled: bool = False

    @classmethod
    def start(
        cls, gh: GitHubClient, state: PinnedState, scan: _ChildScan,
    ) -> _ChildActivation:
        return cls(gh, state, scan, [])

    def consider(self, idx: int, child_number) -> None:
        number = int(child_number)
        if self.scan.labels.get(number) != "blocked":
            return
        pending = self._pending_dependencies(idx)
        if pending:
            self.held.append((number, pending))
        else:
            self.gh.set_workflow_label(
                self.scan.issues[number], WorkflowLabel.READY,
            )
            self.relabeled = True

    def _pending_dependencies(self, idx: int) -> list[int]:
        dep_graph = self.state.get("dep_graph") or {}
        dependencies = dep_graph.get(str(idx), [])
        dep_numbers = [
            int(self.scan.children[int(dep_idx)])
            for dep_idx in dependencies
            if int(dep_idx) < len(self.scan.children)
        ]
        return [
            number for number in dep_numbers
            if self.scan.labels.get(number) != _DONE
        ]


def _activate_ready_children(
    gh: GitHubClient, issue: Issue, state: PinnedState, scan: _ChildScan,
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
    activation = _ChildActivation.start(gh, state, scan)
    for idx, child_number in enumerate(scan.children):
        activation.consider(idx, child_number)
    if activation.relabeled:
        gh.write_pinned_state(issue, state)
    return activation.held


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
    from spamming the tick log. `parent_kind` is `"blocked"` or `_UMBRELLA`.
    """
    from orchestrator import workflow as _wf

    if not held:
        return
    done_count = sum(1 for lbl in child_labels.values() if lbl == _DONE)
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
        if not state.get(_CREATED_AT):
            state.set(_CREATED_AT, _wf._now_iso())
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
        prior = state.get(_LAST_ACTION_COMMENT_ID)
        if not isinstance(prior, int) or latest > prior:
            state.set(_LAST_ACTION_COMMENT_ID, latest)
    gh.set_workflow_label(issue, WorkflowLabel.IMPLEMENTING)
    gh.write_pinned_state(issue, state)
    _wf._handle_implementing(gh, spec, issue)


def _usable_child_scan(
    gh: GitHubClient,
    spec: RepoSpec,
    issue: Issue,
    state: PinnedState,
    children: list,
) -> Optional[_ChildScan]:
    scan = _read_child_labels(gh, issue, children)
    if scan is None:
        return None
    if _park_rejected_children(gh, issue, state, scan.labels):
        return None
    if _park_manually_closed_children(gh, spec, issue, state, scan):
        return None
    return scan


def _handle_empty_blocked_parent(
    gh: GitHubClient, issue: Issue, state: PinnedState,
) -> None:
    from orchestrator import workflow as _wf

    if state.get(_PARENT_NUMBER) or state.get(_AWAITING_HUMAN):
        return
    _wf._park_awaiting_human(
        gh, issue, state,
        f"{config.HITL_MENTIONS} `blocked` without recorded children; "
        "manual relabel suspected.",
        reason="blocked_no_children",
    )
    gh.write_pinned_state(issue, state)


def _complete_blocked_parent(
    gh: GitHubClient, issue: Issue, state: PinnedState,
) -> None:
    from orchestrator import workflow as _wf

    _wf._post_issue_comment(
        gh, issue, state,
        ":white_check_mark: all children resolved; ready for implementation.",
    )
    state.set(_AWAITING_HUMAN, False)
    state.set(_PARK_REASON, None)
    gh.set_workflow_label(issue, WorkflowLabel.READY)
    gh.write_pinned_state(issue, state)


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
    state = gh.read_pinned_state(issue)
    children = state.get(_CHILDREN) or []

    if _route_parent_drift(gh, issue, state):
        return

    if not children:
        _handle_empty_blocked_parent(gh, issue, state)
        return

    scan = _usable_child_scan(gh, spec, issue, state, children)
    if scan is None:
        return
    if all(label == _DONE for label in scan.labels.values()):
        _complete_blocked_parent(gh, issue, state)
        return

    held = _activate_ready_children(gh, issue, state, scan)
    _log_held_children(issue, "blocked", children, scan.labels, held)


def _handle_empty_umbrella(
    gh: GitHubClient, issue: Issue, state: PinnedState,
) -> None:
    from orchestrator import workflow as _wf

    if state.get(_AWAITING_HUMAN):
        return
    _wf._park_awaiting_human(
        gh, issue, state,
        f"{config.HITL_MENTIONS} `umbrella` without recorded children; "
        "manual relabel suspected.",
        reason="umbrella_no_children",
    )
    gh.write_pinned_state(issue, state)


def _complete_umbrella(
    gh: GitHubClient, issue: Issue, state: PinnedState,
) -> None:
    from orchestrator import workflow as _wf

    close_body = ":white_check_mark: all children resolved; closing umbrella issue."
    verdict = _wf._format_issue_usage_verdict(state)
    if verdict:
        close_body = f"{close_body}\n\n{verdict}"
    _wf._post_issue_comment(gh, issue, state, close_body)
    state.set(_AWAITING_HUMAN, False)
    state.set(_PARK_REASON, None)
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


def _handle_umbrella(gh: GitHubClient, spec: RepoSpec, issue: Issue) -> None:
    """Poll children on an umbrella parent that has no implementation of
    its own.

    Mirrors `_handle_blocked` for the rejected/manually-closed checks and
    the dep-graph activation walk, but the all-done branch resolves the
    umbrella to `done` and closes the issue instead of flipping it to
    `ready` -- there is no implementation pass for an umbrella, so the
    only terminal path is "every child resolved -> close".
    """
    state = gh.read_pinned_state(issue)

    # An umbrella parent NEVER enters implementation -- it just closes when
    # every child resolves -- so a body edit cannot be picked up by any
    # later stage's drift check. Route it back to decomposing here so the
    # new manifest is re-derived against the updated body; without this
    # route-back, an edited umbrella would silently close to `done` against
    # the stale manifest once the old children finished.
    if _route_parent_drift(gh, issue, state):
        return

    children = state.get(_CHILDREN) or []
    if not children:
        _handle_empty_umbrella(gh, issue, state)
        return

    scan = _usable_child_scan(gh, spec, issue, state, children)
    if scan is None:
        return
    if all(label == _DONE for label in scan.labels.values()):
        _complete_umbrella(gh, issue, state)
        return

    held = _activate_ready_children(gh, issue, state, scan)
    _log_held_children(issue, _UMBRELLA, children, scan.labels, held)
