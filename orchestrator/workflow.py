# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""State machine: drive issues through the orchestrator workflow.

(no label) -> implementing -> validating -> in_review -> done|rejected.
Validating runs a fresh reviewer session; on changes-requested the dev session
is resumed, the fix pushed, and the review rerun until APPROVED or
MAX_REVIEW_ROUNDS is hit. In_review reacts to PR state (merged/closed) and PR
comments (debounced) and, when AUTO_MERGE is on, merges PRs that the reviewer
approved and that GitHub considers mergeable with green checks. Other labels
are observed and logged as not-yet-implemented.
"""
from __future__ import annotations

import logging
import subprocess  # noqa: F401 -- re-exported so tests can `patch.object(workflow.subprocess, "run", ...)`
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional, Tuple

from github.Issue import Issue

from . import config
from .agents import AgentResult, run_agent
from .config import RepoSpec
from .github import (
    BASE_SYNC_HOLD_LABEL,
    GitHubClient,
    PinnedState,
    issue_has_label,
)
from .workflow_drift import (
    _build_user_content_change_prompt,
    _compute_user_content_hash,
    _detect_user_content_change,
    _mark_drift_comments_consumed,
    _route_drift_to_decomposing,
)
from .workflow_messages import (
    _ORCH_COMMENT_MARKER,
    _build_conflict_resolution_prompt,
    _build_decompose_prompt,
    _build_fix_prompt,
    _build_implement_prompt,
    _build_pr_comment_followup,
    _build_review_prompt,
    _drift_ack_reason,
    _format_stderr_diagnostics,
    _orchestrator_ids,
    _parse_manifest,
    _parse_review_verdict,
    _post_issue_comment,
    _post_pr_comment,
    _recent_comments_text,
    _stderr_log_tail,
)
# Re-exports for backward-compatible `workflow._foo` references in the test
# suite. Each name is the helper module's authoritative definition; the
# redundant `as <name>` aliasing is the pyflakes/ruff convention for marking
# an import as an intentional re-export so F401 does not flag it.
from .workflow_messages import _DRIFT_ACK_RE as _DRIFT_ACK_RE
from .workflow_messages import _has_dep_cycle as _has_dep_cycle
from .workflow_messages import _MANIFEST_RE as _MANIFEST_RE
from .workflow_messages import _MAX_CHILDREN as _MAX_CHILDREN
from .workflow_messages import _ORCH_COMMENT_ID_CAP as _ORCH_COMMENT_ID_CAP
from .workflow_messages import _redact_secrets as _redact_secrets
from .workflow_messages import _REDACT_MIN_VALUE_LEN as _REDACT_MIN_VALUE_LEN
from .workflow_messages import _SECRET_KEY_NAMES as _SECRET_KEY_NAMES
from .workflow_messages import _SECRET_KEY_SUFFIXES as _SECRET_KEY_SUFFIXES
from .workflow_messages import _STDERR_TAIL_BUDGET as _STDERR_TAIL_BUDGET
from .workflow_messages import (
    _track_orchestrator_comment as _track_orchestrator_comment,
)
from .workflow_messages import _VERDICT_RE as _VERDICT_RE
from .workflow_messages import _with_orch_marker as _with_orch_marker
# Re-exports of the git / worktree plumbing. Stage handlers below call these
# names unqualified (`_ensure_worktree(...)`, `_git(...)`, ...) so the
# implementations live in `worktrees.py` but the bindings need to be in this
# module's namespace -- both for the handler call sites and for the test
# suite, which patches `workflow._foo` to intercept those calls.
from .worktrees import _authed_fetch as _authed_fetch
from .worktrees import _branch_ahead_behind as _branch_ahead_behind
from .worktrees import _branch_name as _branch_name
from .worktrees import _cleanup_decompose_worktree as _cleanup_decompose_worktree
from .worktrees import _cleanup_terminal_branch as _cleanup_terminal_branch
from .worktrees import _CONVENTIONAL_RE as _CONVENTIONAL_RE
from .worktrees import _decompose_worktree_path as _decompose_worktree_path
from .worktrees import _ensure_decompose_worktree as _ensure_decompose_worktree
from .worktrees import _ensure_pr_worktree as _ensure_pr_worktree
from .worktrees import _ensure_worktree as _ensure_worktree
from .worktrees import _first_commit_subject as _first_commit_subject
from .worktrees import _git as _git
from .worktrees import _GIT_NO_PROMPT_ENV as _GIT_NO_PROMPT_ENV
from .worktrees import _git_hardened as _git_hardened
from .worktrees import _has_new_commits as _has_new_commits
from .worktrees import _head_sha as _head_sha
from .worktrees import _is_conventional_subject as _is_conventional_subject
from .worktrees import _merge_base_into_worktree as _merge_base_into_worktree
from .worktrees import _PR_REFRESH_DETOUR_LABELS as _PR_REFRESH_DETOUR_LABELS
from .worktrees import _pr_title_from_commit_or_issue as _pr_title_from_commit_or_issue
from .worktrees import _push_branch as _push_branch
from .worktrees import _refresh_base_and_worktrees as _refresh_base_and_worktrees
from .worktrees import _repo_worktrees_root as _repo_worktrees_root
from .worktrees import (
    _route_pr_worktree_to_resolving_conflict as _route_pr_worktree_to_resolving_conflict,
)
from .worktrees import _sanitize_slug as _sanitize_slug
from .worktrees import _SLUG_SAFE_RE as _SLUG_SAFE_RE
from .worktrees import _squash_and_force_push as _squash_and_force_push
from .worktrees import _sync_worktree_with_base as _sync_worktree_with_base
from .worktrees import _worktree_dirty_files as _worktree_dirty_files
from .worktrees import _worktree_path as _worktree_path

log = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _run_agent_tracked(
    gh: GitHubClient,
    issue_number: int,
    *,
    agent_role: str,
    stage: str,
    backend: str,
    prompt: str,
    cwd: Path,
    resume_session_id: Optional[str] = None,
    timeout: Optional[int] = None,
    extra_args: tuple[str, ...] = (),
    review_round: Optional[int] = None,
    retry_count: Optional[int] = None,
) -> AgentResult:
    """Run an agent, bookending the spawn with `agent_spawn` / `agent_exit`
    audit events.

    Thin wrapper around `run_agent` -- the spawn behaviour is unchanged.
    Optional context (`review_round`, `retry_count`, resume session id) is
    forwarded so downstream consumers can correlate spawns with retry
    budgets and reviewer rounds. The exit record carries
    `exit_code`/`timed_out`/`duration_s` from the AgentResult so an
    operator tailing the JSONL sink sees timeouts and crashes without
    needing the orchestrator log too. An exception out of `run_agent`
    propagates -- the audit log will show a spawn without a matching
    exit, which is intentional (the per-issue `tick()` catch above logs
    the traceback).
    """
    start = time.monotonic()
    gh.emit_event(
        "agent_spawn",
        issue_number=issue_number,
        stage=stage,
        agent=backend,
        agent_role=agent_role,
        session_id=resume_session_id,
        review_round=review_round,
        retry_count=retry_count,
    )
    # Forward only the kwargs the original call sites set so the
    # wrapper's run_agent invocation matches the pre-tracking signature
    # call-for-call (test fakes assert on `call.kwargs`).
    run_agent_kwargs: dict[str, Any] = {"extra_args": extra_args}
    if resume_session_id is not None:
        run_agent_kwargs["resume_session_id"] = resume_session_id
    if timeout is not None:
        run_agent_kwargs["timeout"] = timeout
    result = run_agent(backend, prompt, cwd, **run_agent_kwargs)
    duration_s = round(time.monotonic() - start, 3)
    gh.emit_event(
        "agent_exit",
        issue_number=issue_number,
        stage=stage,
        agent=backend,
        agent_role=agent_role,
        session_id=result.session_id,
        duration_s=duration_s,
        exit_code=result.exit_code,
        timed_out=result.timed_out,
        review_round=review_round,
        retry_count=retry_count,
    )
    return result


def _read_dev_session(
    state: PinnedState,
) -> Tuple[str, str, tuple[str, ...], Optional[str]]:
    """Return (spec, backend, extra_args, dev_session_id) for an issue.

    `spec` is the full configured agent command string the next run
    will use -- callers persist it verbatim BEFORE invoking `run_agent`
    so the recorded role identity survives a spawn that returns no
    session id (CLI hiccup, missing output file, etc.). Without that,
    a fresh spawn that nevertheless commits would leave `dev_agent`
    unset and a later `DEV_AGENT` flip would silently retarget the next
    resume at a backend that never ran on this issue.

    The pinned `dev_agent` field stores that spec -- e.g. `"codex"`,
    `"claude"`, or `"codex -m gpt-5.5 -c 'model_reasoning_effort=\"xhigh\"'"`
    -- as the durable role identity. Re-parsing it here means in-flight
    resumes use the same backend AND args the fresh spawn used, even
    after a `DEV_AGENT` env flip between ticks.

    Backward compatibility:
      * Legacy bare-backend values (`"codex"` / `"claude"`) re-parse to
        `(backend, ())` -- no args -- which is what those deployments
        had at the time they were spawned. `spec` is the same bare
        string; persisting it again is a no-op rewrite.
      * Legacy `codex_session_id` (written before `dev_agent` existed)
        yields `spec="codex"`. A config flip to claude cannot strand
        that session -- it stays on codex with no args.
      * When the issue has never been spawned, returns the current
        config's `(DEV_AGENT_SPEC, DEV_AGENT, DEV_AGENT_ARGS, None)`
        for the imminent fresh spawn to use AND persist.
    """
    stored = state.get("dev_agent")
    if stored:
        spec = str(stored)
        backend, args = config._parse_agent_spec("dev_agent", spec)
        sid = state.get("dev_session_id")
        return spec, backend, args, str(sid) if sid is not None else None
    legacy = state.get("codex_session_id")
    if legacy is not None:
        return "codex", "codex", (), str(legacy)
    return (
        config.DEV_AGENT_SPEC,
        config.DEV_AGENT,
        config.DEV_AGENT_ARGS,
        None,
    )


def tick(gh: GitHubClient, spec: RepoSpec) -> None:
    try:
        _refresh_base_and_worktrees(gh, spec)
    except Exception:
        log.exception(
            "repo=%s pre-tick base refresh failed; continuing", spec.slug,
        )
    for issue in gh.list_pollable_issues():
        try:
            _process_issue(gh, spec, issue)
        except Exception:
            log.exception(
                "repo=%s issue=#%s processing failed", spec.slug, issue.number,
            )


def _process_issue(gh: GitHubClient, spec: RepoSpec, issue: Issue) -> None:
    label = gh.workflow_label(issue)
    log.info("repo=%s issue=#%s label=%r", spec.slug, issue.number, label)
    if label is None:
        _handle_pickup(gh, spec, issue)
    elif label == "decomposing":
        _handle_decomposing(gh, spec, issue)
    elif label == "ready":
        _handle_ready(gh, spec, issue)
    elif label == "blocked":
        _handle_blocked(gh, spec, issue)
    elif label == "umbrella":
        _handle_umbrella(gh, spec, issue)
    elif label == "implementing":
        _handle_implementing(gh, spec, issue)
    elif label == "validating":
        _handle_validating(gh, spec, issue)
    elif label == "in_review":
        _handle_in_review(gh, spec, issue)
    elif label == "resolving_conflict":
        _handle_resolving_conflict(gh, spec, issue)
    elif label in ("done", "rejected"):
        return
    else:
        log.warning(
            "repo=%s issue=#%s label=%r not implemented yet; leaving alone",
            spec.slug, issue.number, label,
        )


def _handle_pickup(gh: GitHubClient, spec: RepoSpec, issue: Issue) -> None:
    # Author allowlist: when configured, silently skip unlabeled issues from
    # anyone outside the list so random users can't burn agent budget on a
    # public repo. Maintainers can still drive an outsider's issue manually
    # by adding a workflow label themselves -- the guard only fires here.
    if config.ALLOWED_ISSUE_AUTHORS:
        author = getattr(getattr(issue, "user", None), "login", None) or ""
        # GitHub logins are case-insensitive (Alice and alice resolve to the
        # same account), so normalize both sides before comparing.
        allowed = {h.lower() for h in config.ALLOWED_ISSUE_AUTHORS}
        if author.lower() not in allowed:
            log.info(
                "repo=%s issue=#%s author=%r not in ALLOWED_ISSUE_AUTHORS; skipping pickup",
                spec.slug, issue.number, author,
            )
            return
    state = PinnedState()
    state.set("created_at", _now_iso())
    if config.DECOMPOSE:
        pickup = _post_issue_comment(
            gh, issue, state,
            ":robot: orchestrator picking this up; decomposing.",
        )
        # Anchor the validating-handoff seed-watermark on the exact pickup
        # comment id (see legacy branch comment).
        pickup_id = getattr(pickup, "id", None)
        if pickup_id is not None:
            state.set("pickup_comment_id", int(pickup_id))
        # Snapshot the user-visible content so future ticks can detect a
        # human edit to the title/body mid-flight. Computed AFTER recording
        # the pickup comment id so the pickup itself is filtered out by
        # `_orchestrator_ids` -- otherwise the next tick would include it
        # and the hash would flap once the orchestrator_comment_ids set is
        # consulted there.
        state.set(
            "user_content_hash",
            _compute_user_content_hash(issue, _orchestrator_ids(state)),
        )
        gh.set_workflow_label(issue, "decomposing")
        gh.write_pinned_state(issue, state)
        _handle_decomposing(gh, spec, issue)
        return
    # Legacy path with DECOMPOSE=off: skip decomposition entirely and route
    # the unlabeled issue straight to implementing, exactly as the
    # bootstrap-milestone code did.
    pickup = _post_issue_comment(
        gh, issue, state,
        ":robot: orchestrator picking this up. Decomposition stage is "
        "disabled; going straight to implementation.",
    )
    # Anchor the validating-handoff seed-watermark on the exact pickup
    # comment id. Without this, an issue that started under an older
    # version of the orchestrator (where bot ids were not tracked) would
    # have its first recorded bot id be a much later comment (PR-opened or
    # approval), causing `_seed_watermark_past_self` to silently advance
    # past every issue/PR comment in between -- including any human
    # "do not merge yet" posted during implementing.
    pickup_id = getattr(pickup, "id", None)
    if pickup_id is not None:
        state.set("pickup_comment_id", int(pickup_id))
    state.set(
        "user_content_hash",
        _compute_user_content_hash(issue, _orchestrator_ids(state)),
    )
    gh.set_workflow_label(issue, "implementing")
    gh.write_pinned_state(issue, state)
    _handle_implementing(gh, spec, issue)


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
    last_action_id = state.get("last_action_comment_id")
    new_comments = gh.comments_after(issue, last_action_id)
    if not new_comments:
        return None
    consumed_max = max(c.id for c in new_comments)
    state.set("last_action_comment_id", consumed_max)
    followup = "\n\n".join(
        f"@{c.user.login if c.user else 'user'}: {c.body}"
        for c in new_comments if c.body
    )
    wt = _decompose_worktree_path(spec, issue.number)
    if not wt.exists():
        wt = _ensure_decompose_worktree(spec, issue.number)
    _, decomposer_backend, decomposer_args, decomposer_sid = (
        _read_decomposer_session(state)
    )
    result = _run_agent_tracked(
        gh, issue.number,
        agent_role="decomposer",
        stage="decomposing",
        backend=decomposer_backend,
        prompt=followup,
        cwd=wt,
        resume_session_id=decomposer_sid,
        extra_args=decomposer_args,
        retry_count=state.get("retry_count"),
    )
    state.set("awaiting_human", False)
    return result


def _handle_decomposing(gh: GitHubClient, spec: RepoSpec, issue: Issue) -> None:
    state = gh.read_pinned_state(issue)

    # Track whether to keep the decomposer worktree past this tick. Set
    # True only in the dirty/commits park, where the operator may want to
    # inspect what the agent did. Every other exit (success or park)
    # cleans up via the finally below so the next consumer of this issue
    # number starts from current `origin/<base>`.
    keep_worktree = False
    try:
        # User-content drift FIRST. The spec requires "at the start of
        # every per-tick handler"; running this before the half-finished
        # recovery below is what stops the recovery branch from
        # finalizing to `blocked` / `umbrella` against a stale manifest
        # when the human edited the issue body during a crash window.
        # When drift IS detected we wipe the manifest tracking (children,
        # dep_graph, expected_children_count, umbrella) so the recovery
        # branch is bypassed and the fresh-spawn path below derives a
        # new manifest against the updated body. Previously-created
        # children are listed as orphans in the notice -- they remain
        # on GitHub but the orchestrator no longer tracks them.
        new_hash = _detect_user_content_change(gh, issue, state)
        if new_hash is not None:
            orphans = list(state.get("children") or [])
            if orphans:
                orphan_list = ", ".join(f"#{n}" for n in orphans)
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
            _post_issue_comment(gh, issue, state, notice)
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
            # Fall through: state is now clean (no children, no session),
            # so the half-finished recovery below is bypassed, the
            # awaiting-human branch is bypassed, and the fresh-spawn
            # branch runs the decomposer this tick.

        # Half-finished decomposition recovery. Two persistent markers
        # signal a prior tick crashed mid-split:
        #   * `expected_children_count` is written BEFORE any child is
        #     created, so a SIGKILL after `create_child_issue` returns
        #     but before the parent records the new child number leaves
        #     the parent with this marker AND zero recorded children
        #     while an orphan child issue exists on GitHub. Re-running
        #     the decomposer here would emit a different manifest and
        #     create duplicate children alongside the orphan.
        #   * `children` is written incrementally after each successful
        #     create + parent-state flush. Its presence covers a crash
        #     after at least one child was recorded.
        # Either marker present without the parent label having flipped
        # to `blocked` means we cannot safely respawn the decomposer.
        # Branch by whether the recorded count matches expectations:
        # equal -> finalize to `blocked`; less -> park awaiting human.
        # Legacy state from a deploy that pre-dates
        # `expected_children_count` still routes through the
        # `children`-only branch and finalizes.
        expected_raw = state.get("expected_children_count")
        children_recorded = state.get("children") or []
        if expected_raw is not None or children_recorded:
            if state.get("awaiting_human"):
                return
            if expected_raw is not None and len(children_recorded) < int(
                expected_raw
            ):
                _park_awaiting_human(
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
                return
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
                            child_state.set("created_at", _now_iso())
                        child_state.set("awaiting_human", False)
                        child_state.set("park_reason", None)
                        gh.write_pinned_state(child_issue, child_state)
                except Exception:
                    log.exception(
                        "issue=#%s could not repair orphan child #%s during "
                        "decomposition recovery", issue.number, child_number,
                    )
                    _park_awaiting_human(
                        gh, issue, state,
                        f"{config.HITL_MENTIONS} could not repair child "
                        f"#{child_number} during decomposition recovery "
                        "(seed `parent_number` on its pinned state); manual "
                        "intervention needed (check orchestrator logs).",
                        reason="child_seed_failed",
                    )
                    gh.write_pinned_state(issue, state)
                    return
            # `umbrella=True` is persisted alongside `expected_children_count`
            # before any child is created, so the recovery path here picks
            # it up and finalizes to `umbrella` instead of `blocked`. Without
            # this branch, a SIGKILL between the umbrella manifest's child
            # creation loop and the final label flip would resume as a
            # plain blocked parent and re-enter implementation after all
            # children resolved -- the opposite of what the manifest asked.
            finalize_label = (
                "umbrella" if state.get("umbrella") else "blocked"
            )
            gh.set_workflow_label(issue, finalize_label)
            gh.write_pinned_state(issue, state)
            return

        # DECOMPOSE kill-switch bailout. Every path below this point
        # spawns the decomposer (fresh or via the awaiting_human
        # resume), so an operator who restarts with DECOMPOSE=off after
        # `_handle_pickup` already labeled the issue `decomposing` --
        # or while it is parked there awaiting a human -- would still
        # see the disabled rollout create manifests and child issues.
        # Drop into the legacy implementing flow exactly as
        # `_handle_pickup` does on a freshly unlabeled issue. The
        # half-finished recovery above must keep running regardless of
        # the flag: abandoning orphan children (already on GitHub)
        # because new decompositions are now disabled would strand
        # work, which is not what a kill switch should do.
        if not config.DECOMPOSE:
            _post_issue_comment(
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
            gh.set_workflow_label(issue, "implementing")
            gh.write_pinned_state(issue, state)
            _handle_implementing(gh, spec, issue)
            return

        # (User-content drift handled at the top of the try block above
        # so it runs BEFORE half-finished recovery -- otherwise the
        # recovery branch would finalize against a stale manifest when
        # the issue was edited during a crash window.)

        if state.get("awaiting_human"):
            result = _resume_decomposer_on_human_reply(gh, spec, issue, state)
            if result is None:
                # No human reply yet. Keep the worktree intact -- if a
                # prior tick parked on the dirty/commits reason, the
                # HITL message explicitly asks the operator to inspect
                # and reset it before resuming, and cleanup here would
                # silently delete that state on every subsequent poll.
                keep_worktree = True
                return
        else:
            if not _check_and_increment_retry_budget(
                gh, issue, state, stage="decomposing"
            ):
                gh.write_pinned_state(issue, state)
                return
            wt = _ensure_decompose_worktree(spec, issue.number)
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
            prompt = _build_decompose_prompt(issue, _recent_comments_text(issue))
            result = _run_agent_tracked(
                gh, issue.number,
                agent_role="decomposer",
                stage="decomposing",
                backend=decomposer_backend,
                prompt=prompt,
                cwd=wt,
                extra_args=decomposer_args,
                retry_count=state.get("retry_count"),
            )
            if result.session_id:
                state.set("decomposer_session_id", result.session_id)

        state.set("last_agent_action_at", _now_iso())

        if result.timed_out:
            _park_awaiting_human(
                gh, issue, state,
                f"{config.HITL_MENTIONS} decomposer timed out after "
                f"{config.AGENT_TIMEOUT}s, manual intervention needed.",
                reason="decomposer_timeout",
            )
            gh.write_pinned_state(issue, state)
            return

        # The decomposer is supposed to be read-only. If it committed or
        # left uncommitted changes, something has gone wrong (prompt
        # ignored, agent misbehaving, operator scratch). Park awaiting
        # human and KEEP the worktree past this tick so the operator can
        # inspect what the decomposer actually produced before resetting.
        wt = _decompose_worktree_path(spec, issue.number)
        if _has_new_commits(spec, wt) or _worktree_dirty_files(wt):
            keep_worktree = True
            _park_awaiting_human(
                gh, issue, state,
                f"{config.HITL_MENTIONS} decomposer left commits or uncommitted "
                "changes in the worktree, but it must be read-only. Reset the "
                "worktree before resuming.",
                reason="decomposer_dirty",
            )
            gh.write_pinned_state(issue, state)
            return

        last_msg = result.last_message or ""
        parsed, error = _parse_manifest(last_msg)

        if parsed is None:
            # Either malformed manifest OR no manifest at all (question /
            # silence). Both park awaiting human; resume on the next
            # comment runs through the awaiting_human branch above.
            if error is not None:
                quoted = "> " + last_msg.strip().replace("\n", "\n> ")
                _park_awaiting_human(
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
                    else _format_stderr_diagnostics(result, "Decomposer")
                )
                _park_awaiting_human(
                    gh, issue, state,
                    f"{config.HITL_MENTIONS} decomposer needs your input to "
                    f"proceed:\n\n{quoted}{diag}",
                    reason="decomposer_silent" if not stripped else "decomposer_question",
                )
                if not stripped:
                    log.warning(
                        "issue=#%s decomposer produced no final message; "
                        "exit_code=%d timed_out=%s stderr_tail=%r",
                        issue.number, result.exit_code, result.timed_out,
                        _stderr_log_tail(result),
                    )
            gh.write_pinned_state(issue, state)
            return

        if parsed["decision"] == "single":
            # `_parse_manifest` only checks the decision string for the
            # single branch, so `rationale` may be any JSON value (or
            # missing). Coerce non-strings to the placeholder rather than
            # crashing the handler at `.strip()` after the agent already ran.
            raw_rationale = parsed.get("rationale")
            if not isinstance(raw_rationale, str):
                raw_rationale = ""
            rationale = raw_rationale.strip() or "(no rationale provided)"
            _post_issue_comment(
                gh, issue, state,
                f":mag: decomposer says this fits one context: {rationale}",
            )
            state.set("decomposed_at", _now_iso())
            gh.set_workflow_label(issue, "ready")
            gh.write_pinned_state(issue, state)
            return

        # decision == "split". Crash-safe sequence:
        #   1. Persist `expected_children_count` BEFORE creating any
        #      child. The half-finished recovery uses this to tell a
        #      partial loop apart from a completed one.
        #   2. For each child: create the GitHub issue, then
        #      IMMEDIATELY record its number in parent state (before
        #      any further non-idempotent work). A SIGKILL between
        #      these two steps is unavoidable; persisting first means
        #      the worst case is an orphan child without seeded
        #      `parent_number`, not a duplicate child created by a
        #      decomposer respawn.
        #   3. Seed child pinned state. Failure here parks but parent
        #      state already records the child, so no respawn happens.
        #   4. After the loop: post the summary, label parent
        #      `blocked`. Activation (children blocked -> ready) only
        #      runs AFTER this final write, so a crash here cannot
        #      leave a runnable orphan child against a
        #      `decomposing`-labeled parent.
        children_manifest = parsed["children"]
        is_umbrella = bool(parsed.get("umbrella"))
        created: list[Tuple[int, dict]] = []
        dep_graph: dict[str, list[int]] = {}
        state.set("expected_children_count", len(children_manifest))
        # Persist the umbrella flag alongside the count so the half-finished
        # recovery path above can finalize to the right label after a
        # mid-loop SIGKILL. Always write it (including when False) so a
        # buggy state migration that left a stale True from a prior aborted
        # decomposition cannot survive into the recovery branch.
        state.set("umbrella", is_umbrella)
        gh.write_pinned_state(issue, state)
        for idx, child in enumerate(children_manifest):
            depends_on = list(child.get("depends_on") or [])
            try:
                new_issue = gh.create_child_issue(
                    title=child["title"],
                    body=child["body"],
                    parent_number=issue.number,
                    labels=["blocked"],
                )
            except Exception:
                log.exception(
                    "issue=#%s could not create child %d (%r)",
                    issue.number, idx, child.get("title"),
                )
                _park_awaiting_human(
                    gh, issue, state,
                    f"{config.HITL_MENTIONS} could not create child issue "
                    f"index={idx} ({child.get('title')!r}); manual intervention "
                    "needed (check orchestrator logs).",
                    reason="child_create_failed",
                )
                gh.write_pinned_state(issue, state)
                return

            # Persist the child number on the parent BEFORE doing any
            # further work for this child. A SIGKILL between
            # `create_child_issue` returning and this write would leave
            # an orphan child on GitHub that the parent does not know
            # about; the next tick would re-spawn the decomposer and
            # create duplicates.
            created.append((new_issue.number, child))
            if depends_on:
                dep_graph[str(idx)] = depends_on
            state.set("children", [n for n, _ in created])
            if dep_graph:
                state.set("dep_graph", dep_graph)
            state.set("decomposed_at", _now_iso())
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
                child_state.set("created_at", _now_iso())
                gh.write_pinned_state(new_issue, child_state)
            except Exception:
                log.exception(
                    "issue=#%s could not seed pinned state on child #%d",
                    issue.number, new_issue.number,
                )
                # Parent already records the child (no duplicate
                # risk). Park so a human can either seed the child
                # manually or close it.
                _park_awaiting_human(
                    gh, issue, state,
                    f"{config.HITL_MENTIONS} created child #{new_issue.number} "
                    f"({child.get('title')!r}) but could not seed its pinned "
                    "state with `parent_number`; manual intervention needed "
                    "(seed parent_number on the child or close it).",
                    reason="child_seed_failed",
                )
                gh.write_pinned_state(issue, state)
                return

        # children/dep_graph/decomposed_at are already durable from the
        # incremental writes in the loop above. Post the summary, flip
        # the parent label to `blocked` (or `umbrella` when the parent
        # has no implementation work of its own), and persist the new
        # orchestrator_comment_id. Activation (children blocked -> ready)
        # only runs AFTER this final write, so a crash here cannot leave
        # a runnable orphan child against a `decomposing`-labeled parent.
        summary = "\n".join(
            f"- #{n}: {child['title']}" for n, child in created
        )
        if is_umbrella:
            summary_intro = (
                f":bookmark_tabs: decomposer split this into {len(created)} "
                f"child issue(s); marking parent as `umbrella` (no "
                f"implementation of its own; will auto-resolve once every "
                f"child resolves):\n\n{summary}"
            )
            final_label = "umbrella"
        else:
            summary_intro = (
                f":bookmark_tabs: decomposer split this into {len(created)} "
                f"child issue(s):\n\n{summary}"
            )
            final_label = "blocked"
        _post_issue_comment(gh, issue, state, summary_intro)
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
                gh.set_workflow_label(child_issue, "ready")
            except Exception:
                log.exception(
                    "issue=#%s could not flip child #%d to ready; the parent's "
                    "_handle_blocked walk will retry on the next tick",
                    issue.number, child_number,
                )
    finally:
        if not keep_worktree:
            _cleanup_decompose_worktree(spec, issue.number)


def _handle_ready(gh: GitHubClient, spec: RepoSpec, issue: Issue) -> None:
    """`ready` is the entry point for an auto-created child or for a parent
    whose decomposer voted `single`. Both cases need the same pickup-state
    seeding the legacy `_handle_pickup` did before flipping to
    `implementing`, so the validating handoff watermark and the in_review
    legacy migration have an anchor comment they can key on.
    """
    state = gh.read_pinned_state(issue)
    # User-content drift before implementation has started: route back to
    # decomposing so the manifest is re-derived against the new body. A
    # non-umbrella parent can reach `ready` after every child resolves
    # (`_handle_blocked`'s all-done branch flips `blocked` -> `ready`),
    # so the parent may STILL carry `children` / `dep_graph` /
    # `expected_children_count` from the prior manifest. Without clearing
    # those, the next `_handle_decomposing` tick's half-finished
    # recovery branch would fire and just flip the issue back to
    # `blocked` without re-running the decomposer. Route via
    # `_route_drift_to_decomposing` so the manifest tracking is wiped
    # alongside the locked decomposer session; the now-resolved children
    # are listed in the notice as orphans so the operator can close any
    # that no longer apply to the updated requirements.
    new_hash = _detect_user_content_change(gh, issue, state)
    if new_hash is not None:
        orphans = list(state.get("children") or [])
        _route_drift_to_decomposing(gh, issue, state, new_hash, orphans)
        gh.write_pinned_state(issue, state)
        return
    if state.get("pickup_comment_id") is None:
        if not state.get("created_at"):
            state.set("created_at", _now_iso())
        pickup = _post_issue_comment(
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
    gh.set_workflow_label(issue, "implementing")
    gh.write_pinned_state(issue, state)
    _handle_implementing(gh, spec, issue)


def _handle_blocked(gh: GitHubClient, spec: RepoSpec, issue: Issue) -> None:
    """Poll children to decide whether the parent unblocks (or one of the
    children unblocks).

    Workers run sequentially in the polling loop today, so the child's
    `in_review -> done` label flip and this tick cannot truly race; we read
    each child's current label fresh here.
    """
    state = gh.read_pinned_state(issue)
    children = state.get("children") or []

    # User-content drift detection. The hash baseline is initialized by
    # `_detect_user_content_change` itself on the first encounter, so a
    # legacy `blocked` issue still missing the field is durably seeded
    # here (via the helper's own `write_pinned_state`) rather than
    # silently absorbing the next edit as the new baseline. Per the spec
    # ("Before validating: route back to decomposing"), both parent and
    # child cases route to decomposing -- silently persisting the new
    # baseline for a child would let `_handle_ready` later see a matching
    # hash and skip the re-decomposer, even when the edited body now
    # needs splitting. Parents with in-flight children list those
    # children as orphans in the notice (the new manifest may overlap
    # with them; the operator closes the obsolete ones manually).
    new_hash = _detect_user_content_change(gh, issue, state)
    if new_hash is not None:
        _route_drift_to_decomposing(
            gh, issue, state, new_hash, list(children),
        )
        gh.write_pinned_state(issue, state)
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
        _park_awaiting_human(
            gh, issue, state,
            f"{config.HITL_MENTIONS} `blocked` without recorded children; "
            "manual relabel suspected.",
            reason="blocked_no_children",
        )
        gh.write_pinned_state(issue, state)
        return

    child_labels: dict[int, Optional[str]] = {}
    child_issues: dict[int, Issue] = {}
    for child_number in children:
        try:
            child_issue = gh.get_issue(int(child_number))
        except Exception:
            log.exception(
                "issue=#%s could not read child #%d", issue.number, child_number,
            )
            return
        child_issues[int(child_number)] = child_issue
        child_labels[int(child_number)] = gh.workflow_label(child_issue)

    rejected = [n for n, lbl in child_labels.items() if lbl == "rejected"]
    if rejected:
        if state.get("awaiting_human"):
            return
        _park_awaiting_human(
            gh, issue, state,
            f"{config.HITL_MENTIONS} child issue(s) rejected: "
            f"{', '.join(f'#{n}' for n in rejected)}; "
            "decide whether to re-decompose or close.",
            reason="child_rejected",
        )
        gh.write_pinned_state(issue, state)
        return

    # A child closed manually (e.g. via the GitHub UI) before reaching
    # `in_review` is invisible to `list_pollable_issues`, which only
    # sweeps closed issues for `in_review` (the externally-merged
    # path). Its workflow label stays frozen at whatever it was at
    # close -- ready/blocked/implementing/validating, or none at all
    # -- so without this branch the parent would read the stale label,
    # neither the rejected nor the all-done branch would fire, and the
    # parent would wait forever for a child that is gone. Treat it
    # like a rejected child so the operator can adjudicate. `in_review`
    # is intentionally allowed: a state=closed/label=in_review child is
    # the externally-merged transient that the closed-in_review sweep
    # finalizes on the next tick, NOT a manual override.
    manually_closed = [
        n for n, ci in child_issues.items()
        if getattr(ci, "state", "open") == "closed"
        and child_labels.get(n) not in ("done", "rejected", "in_review")
    ]
    if manually_closed:
        if state.get("awaiting_human"):
            return
        _park_awaiting_human(
            gh, issue, state,
            f"{config.HITL_MENTIONS} child issue(s) closed without reaching "
            f"`done` or `rejected`: "
            f"{', '.join(f'#{n}' for n in manually_closed)}; "
            "decide whether to re-decompose or close.",
            reason="child_manually_closed",
        )
        gh.write_pinned_state(issue, state)
        return

    if all(lbl == "done" for lbl in child_labels.values()):
        _post_issue_comment(
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
        gh.set_workflow_label(issue, "ready")
        gh.write_pinned_state(issue, state)
        return

    # Walk children: any `blocked` child whose recorded dependencies are
    # all `done` gets relabeled `ready`. A child with no recorded deps
    # also flips (vacuous all-done over an empty list) -- this recovers
    # any no-dep child that the decomposer's same-tick activation step
    # left as `blocked` (network blip, label-flip failure, etc.).
    dep_graph = state.get("dep_graph") or {}
    relabeled = False
    for idx, child_number in enumerate(children):
        cn = int(child_number)
        if child_labels.get(cn) != "blocked":
            continue
        deps = dep_graph.get(str(idx), [])
        dep_numbers = [
            int(children[int(d)]) for d in deps if int(d) < len(children)
        ]
        if all(child_labels.get(dn) == "done" for dn in dep_numbers):
            gh.set_workflow_label(child_issues[cn], "ready")
            relabeled = True
    if relabeled:
        gh.write_pinned_state(issue, state)


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

    # User-content drift detection. An umbrella parent NEVER enters
    # implementation -- it just closes when every child resolves -- so a
    # body edit cannot be picked up by any later stage's drift check.
    # Per the spec ("Before validating: route back to decomposing"), the
    # umbrella is routed back to decomposing so the new manifest is
    # re-derived against the updated body. The previously-tracked
    # children become orphans on GitHub (the orchestrator no longer
    # tracks them); the notice lists them explicitly so the operator can
    # close any that no longer apply. Without this route-back, an
    # edited umbrella would silently close to `done` against the stale
    # manifest once the old children finished.
    new_hash = _detect_user_content_change(gh, issue, state)
    if new_hash is not None:
        orphans = list(state.get("children") or [])
        _route_drift_to_decomposing(gh, issue, state, new_hash, orphans)
        gh.write_pinned_state(issue, state)
        return

    children = state.get("children") or []
    if not children:
        # An umbrella with no recorded children is corrupt state (the
        # decomposer only applies the umbrella label after creating
        # children), but still surface to a human rather than silently
        # closing an issue with no aggregated work.
        if state.get("awaiting_human"):
            return
        _park_awaiting_human(
            gh, issue, state,
            f"{config.HITL_MENTIONS} `umbrella` without recorded children; "
            "manual relabel suspected.",
            reason="umbrella_no_children",
        )
        gh.write_pinned_state(issue, state)
        return

    child_labels: dict[int, Optional[str]] = {}
    child_issues: dict[int, Issue] = {}
    for child_number in children:
        try:
            child_issue = gh.get_issue(int(child_number))
        except Exception:
            log.exception(
                "issue=#%s could not read child #%d", issue.number, child_number,
            )
            return
        child_issues[int(child_number)] = child_issue
        child_labels[int(child_number)] = gh.workflow_label(child_issue)

    rejected = [n for n, lbl in child_labels.items() if lbl == "rejected"]
    if rejected:
        if state.get("awaiting_human"):
            return
        _park_awaiting_human(
            gh, issue, state,
            f"{config.HITL_MENTIONS} child issue(s) rejected: "
            f"{', '.join(f'#{n}' for n in rejected)}; "
            "decide whether to re-decompose or close.",
            reason="child_rejected",
        )
        gh.write_pinned_state(issue, state)
        return

    manually_closed = [
        n for n, ci in child_issues.items()
        if getattr(ci, "state", "open") == "closed"
        and child_labels.get(n) not in ("done", "rejected", "in_review")
    ]
    if manually_closed:
        if state.get("awaiting_human"):
            return
        _park_awaiting_human(
            gh, issue, state,
            f"{config.HITL_MENTIONS} child issue(s) closed without reaching "
            f"`done` or `rejected`: "
            f"{', '.join(f'#{n}' for n in manually_closed)}; "
            "decide whether to re-decompose or close.",
            reason="child_manually_closed",
        )
        gh.write_pinned_state(issue, state)
        return

    if all(lbl == "done" for lbl in child_labels.values()):
        _post_issue_comment(
            gh, issue, state,
            ":white_check_mark: all children resolved; closing umbrella issue.",
        )
        state.set("awaiting_human", False)
        state.set("park_reason", None)
        state.set("umbrella_resolved_at", _now_iso())
        gh.set_workflow_label(issue, "done")
        gh.write_pinned_state(issue, state)
        try:
            issue.edit(state="closed")
        except Exception:
            log.exception(
                "issue=#%s could not close umbrella after children done",
                issue.number,
            )
        return

    # Same dep-graph activation walk as `_handle_blocked`: an umbrella's
    # children can still depend on each other, and a no-dep child stuck
    # at `blocked` after a same-tick activation hiccup needs to be
    # rescued here.
    dep_graph = state.get("dep_graph") or {}
    relabeled = False
    for idx, child_number in enumerate(children):
        cn = int(child_number)
        if child_labels.get(cn) != "blocked":
            continue
        deps = dep_graph.get(str(idx), [])
        dep_numbers = [
            int(children[int(d)]) for d in deps if int(d) < len(children)
        ]
        if all(child_labels.get(dn) == "done" for dn in dep_numbers):
            gh.set_workflow_label(child_issues[cn], "ready")
            relabeled = True
    if relabeled:
        gh.write_pinned_state(issue, state)


def _park_awaiting_human(
    gh: GitHubClient, issue: Issue, state: PinnedState, message: str,
    *,
    reason: Optional[str] = None,
) -> None:
    """Post `message` and mark the issue as awaiting a human reply.

    Caller is responsible for `gh.write_pinned_state` afterwards (mirrors the
    existing _on_question / _on_dirty_worktree contract). Clears any stale
    `park_reason` -- a transient AUTO_MERGE park (failed_checks/unmergeable)
    followed by a follow-up question/timeout park would otherwise leave
    the transient reason behind and let the in_review recovery branch
    auto-merge over the dev's standing question on the next tick. Callers
    that re-park for a transient reason (the AUTO_MERGE failed-checks /
    unmergeable paths) re-set `park_reason` immediately after this call.

    `reason` is recorded only in the emitted `park_awaiting_human` audit
    event; the durable `park_reason` field in pinned state is still cleared
    here (callers that need a transient reason re-set it themselves -- see
    above), so passing a reason does not change observable behavior.
    """
    _post_issue_comment(gh, issue, state, message)
    state.set("awaiting_human", True)
    state.set("park_reason", None)
    latest = gh.latest_comment_id(issue)
    if latest is not None:
        state.set("last_action_comment_id", latest)
    # Read the label AFTER the comment post and state writes so the
    # captured stage reflects the handler that drove the park (the label
    # itself is unchanged by this call -- callers relabel only after the
    # `write_pinned_state` they do next).
    gh.emit_event(
        "park_awaiting_human",
        issue_number=issue.number,
        stage=gh.workflow_label(issue),
        reason=reason,
    )


def _check_and_increment_retry_budget(
    gh: GitHubClient,
    issue: Issue,
    state: PinnedState,
    *,
    stage: str = "implementing",
) -> bool:
    """Gate fresh agent spawns by a per-issue 24h retry cap.

    The window starts at the first counted attempt and resets once 24h after
    that start has elapsed -- a fixed window per issue, not a true rolling
    window, but enough to stop a stuck issue from burning tokens for a day.
    Implementing and decomposing share the same per-issue counter on
    purpose: both consume the issue's daily spawn budget.

    Returns True if the spawn is allowed (and the budget was incremented);
    False if the cap is exhausted (and the issue was parked on awaiting_human).

    Only fresh spawns count. Resumes on human reply and recovered-worktree
    pushes are explicit unblock signals or carry-over work, not retries.
    Caller writes pinned state after this returns; on the False branch we have
    already parked, so caller's pinned-state write commits the park.
    """
    cap = config.MAX_RETRIES_PER_DAY
    if cap <= 0:
        return True

    now = datetime.now(timezone.utc)
    window_start_raw = state.get("retry_window_start")
    window_start: Optional[datetime] = None
    if window_start_raw:
        try:
            window_start = datetime.fromisoformat(window_start_raw)
        except (TypeError, ValueError):
            window_start = None

    if window_start is None or now - window_start > timedelta(hours=24):
        # Window absent/corrupt/expired: open a new one.
        state.set("retry_window_start", _now_iso())
        state.set("retry_count", 0)
        window_start_raw = state.get("retry_window_start")

    count = int(state.get("retry_count") or 0)
    if count >= cap:
        _park_awaiting_human(
            gh, issue, state,
            f"{config.HITL_MENTIONS} hit retry cap ({cap}/day) for "
            f"{stage}; manual intervention needed. "
            f"Window opened at {window_start_raw}.",
            reason="retry_cap",
        )
        return False

    state.set("retry_count", count + 1)
    return True


# After this many consecutive `agent_silent` parks on the same
# `dev_session_id`, `_resume_dev_with_text` drops the session id and starts
# a fresh spawn. Two strikes (rather than one) tolerates a transient
# single-call blip while still preventing the resume loop from burning every
# fresh-spawn retry slot on a poisoned session that's not coming back.
_SILENT_PARKS_BEFORE_FRESH_SESSION = 2

# Substrings Claude's CLI prints to stderr when `--resume <sid>` references a
# session that no longer exists (transcript GC'd, a different host, a
# mid-stream kill, etc.). This is a deterministic, recoverable failure --
# unlike a transient API blip -- so `_resume_dev_with_text` retries once
# immediately with a cleared session id instead of waiting for the silent-
# park counter to climb to `_SILENT_PARKS_BEFORE_FRESH_SESSION`.
#
# Kept as a tuple of lowercase substrings so phrasing tweaks across Claude
# CLI releases ("No conversation found ..." / "No conversation with ID ..."
# / "Conversation ... not found") still match.
_CLAUDE_STALE_SESSION_STDERR_MARKERS: Tuple[str, ...] = (
    "no conversation found with session id",
    "no conversation found with id",
    "no conversation with session id",
    "conversation not found",
)


def _is_stale_session_failure(backend: str, result: AgentResult) -> bool:
    """True iff `result` is a deterministic stale-session resume failure.

    Only claude is matched today: codex's resume CLI does not expose a
    comparable stable stderr marker, so codex still relies on the silent-
    park-count fallback. If/when codex grows one, add it here.
    """
    if backend != "claude":
        return False
    stderr = (result.stderr or "").lower()
    if not stderr:
        return False
    return any(marker in stderr for marker in _CLAUDE_STALE_SESSION_STDERR_MARKERS)


def _drop_poisoned_dev_session(state: PinnedState) -> None:
    """Clear the pinned dev session id (and legacy `codex_session_id`).

    Preserves the stored `dev_agent` spec when one is already pinned --
    a poisoned session is a transcript problem, not a backend-selection
    problem, so the fresh spawn that follows must replay the exact same
    backend+args. Writing the parsed backend back here would silently
    strip the configured CLI args from the spec and switch a `codex -m
    gpt-5.5 -c '...'` issue back to bare `codex` on the next resume.

    When the issue is on the legacy `codex_session_id` schema (no
    `dev_agent` ever written), pin `dev_agent="codex"` BEFORE clearing
    the legacy field. Without this, the next `_read_dev_session` would
    fall through to the config default and a `DEV_AGENT=claude` flip
    would silently switch the issue from codex to claude on retry.

    Clearing the legacy field too leaves no trace of the dropped
    session anywhere.
    """
    if not state.get("dev_agent") and state.get("codex_session_id") is not None:
        state.set("dev_agent", "codex")
    state.set("dev_session_id", None)
    state.set("codex_session_id", None)
    state.set("silent_park_count", 0)


def _resume_dev_with_text(
    gh: GitHubClient,
    spec: RepoSpec,
    issue: Issue,
    state: PinnedState,
    followup_text: str,
) -> Tuple[Path, AgentResult]:
    """Resume the dev's locked-backend session with the given prompt text.

    The backend is locked to whatever wrote `dev_session_id` (or the legacy
    `codex_session_id`) for this issue -- resuming across backends would need
    an inter-backend session bridge that does not exist. Clears the
    `awaiting_human` flag because the caller is reacting to a fresh human
    signal (issue or PR comment) by spawning the agent.

    After `_SILENT_PARKS_BEFORE_FRESH_SESSION` consecutive `agent_silent`
    parks on the current `dev_session_id`, the resume drops the session id
    and starts a fresh spawn instead. Sessions killed mid-stream (e.g. by a
    Claude rate limit) consistently return empty results on every subsequent
    resume; without this fallback every human "retry" comment burns another
    fresh-spawn retry slot on the same poisoned session.

    A Claude resume that comes back with `No conversation found with session
    ID` (or a sibling marker) is treated as the same poisoned-session
    condition but recognized immediately: the pinned session id is cleared
    and the call is retried once as a fresh spawn in the same worktree, so
    a `resolving_conflict` awaiting-human resume on a Claude session whose
    transcript was GC'd doesn't park `agent_silent` for two ticks before
    recovering.
    """
    wt = _worktree_path(spec, issue.number)
    if not wt.exists():
        wt = _ensure_worktree(spec, issue.number)
    _, dev_backend, dev_args, dev_sid = _read_dev_session(state)
    silent_count = int(state.get("silent_park_count") or 0)
    fresh_spawn = (
        dev_sid is not None
        and silent_count >= _SILENT_PARKS_BEFORE_FRESH_SESSION
    )
    if fresh_spawn:
        log.info(
            "issue=#%d dropping poisoned dev session %r after %d "
            "consecutive silent parks; starting fresh",
            issue.number, dev_sid, silent_count,
        )
        dev_sid = None
        # Clear the poisoned session from pinned state BEFORE the spawn.
        # If the fresh spawn returns no `session_id` (or its persistence
        # is racy), the next tick must see a cleared session -- not the
        # old poisoned id, which `_read_dev_session` would otherwise
        # return again and burn another retry.
        _drop_poisoned_dev_session(state)
    # Stage context reflects the current label so events from validating /
    # in_review / resolving_conflict resumes (or implementing awaiting-human
    # resumes) are tagged with the handler that triggered the resume.
    resume_stage = gh.workflow_label(issue) or "implementing"
    result = _run_agent_tracked(
        gh, issue.number,
        agent_role="developer",
        stage=resume_stage,
        backend=dev_backend,
        prompt=followup_text,
        cwd=wt,
        resume_session_id=dev_sid,
        extra_args=dev_args,
        review_round=state.get("review_round"),
        retry_count=state.get("retry_count"),
    )

    # Deterministic stale-session recovery: if we resumed with a session id
    # and Claude responded with the "no conversation found" marker, the
    # pinned session is dead. Drop it and retry once as a fresh spawn in the
    # same worktree so the caller (typically resolving_conflict awaiting-
    # human) sees a real agent result on this tick instead of a silent park.
    # Bounded to one retry: if the fresh spawn ALSO trips a stale-session
    # marker something deeper is wrong (e.g. a misconfigured CLI) and we
    # surface that result rather than looping.
    if (
        dev_sid is not None
        and not fresh_spawn
        and _is_stale_session_failure(dev_backend, result)
    ):
        log.info(
            "issue=#%d dropping poisoned dev session %r after stale-session "
            "stderr marker; retrying once as a fresh spawn",
            issue.number, dev_sid,
        )
        _drop_poisoned_dev_session(state)
        fresh_spawn = True
        result = _run_agent_tracked(
            gh, issue.number,
            agent_role="developer",
            stage=resume_stage,
            backend=dev_backend,
            prompt=followup_text,
            cwd=wt,
            resume_session_id=None,
            extra_args=dev_args,
            review_round=state.get("review_round"),
            retry_count=state.get("retry_count"),
        )

    if fresh_spawn and result.session_id:
        # Fresh spawn produced a session id -- record it so subsequent
        # resumes pick up the live session. Mirrors the persistence done
        # in `_handle_implementing`'s fresh-spawn branch.
        state.set("dev_session_id", result.session_id)
    state.set("awaiting_human", False)
    return wt, result


def _resume_developer_on_human_reply(
    gh: GitHubClient, spec: RepoSpec, issue: Issue, state: PinnedState
) -> Optional[Tuple[Path, AgentResult]]:
    """Resume the developer's agent session with new issue-level comments.

    Returns (worktree, agent_result) on resume, or None if there are no new
    comments since the last park (caller should return without writing state).

    Used by `implementing` and `validating` -- both deliberately watch only
    the issue's comment thread, not the PR's. The `in_review` handler watches
    PR comments too via `_resume_dev_with_text` directly.

    Bumps `last_action_comment_id` to the highest consumed comment id BEFORE
    spawning the agent. Without this, a successful resume during implementing
    or validating leaves `last_action_comment_id` at the prior park id, so
    the validating->in_review handoff treats the just-consumed human reply
    as fresh PR feedback and re-resumes the dev on input it has already
    handled. This pre-resume bump is also robust to mid-resume failures:
    if the agent crashes or times out, those comments are still recorded
    as consumed (the dev DID see them via the resume prompt), and the
    failure is surfaced via the timeout/dirty/question paths instead.
    """
    last_action_id = state.get("last_action_comment_id")
    new_comments = gh.comments_after(issue, last_action_id)
    if not new_comments:
        return None
    consumed_max = max(c.id for c in new_comments)
    state.set("last_action_comment_id", consumed_max)
    followup = "\n\n".join(
        f"@{c.user.login if c.user else 'user'}: {c.body}"
        for c in new_comments if c.body
    )
    return _resume_dev_with_text(gh, spec, issue, state, followup)


def _handle_implementing(gh: GitHubClient, spec: RepoSpec, issue: Issue) -> None:
    state = gh.read_pinned_state(issue)

    # User-content drift: a human edited the issue title/body after the dev
    # session was spawned. The issue spec ("don't re-decompose mid-
    # implementation -- too disruptive") rules out routing back to
    # `decomposing` here; instead notify the human and resume the locked
    # dev session with the new body so it can decide what to do. When no
    # dev session exists yet (fresh `ready` -> `implementing` bounce that
    # hasn't spawned), just persist the new hash and let the fresh-spawn
    # branch below pick the new body up via `_build_implement_prompt`.
    new_hash = _detect_user_content_change(gh, issue, state)
    if new_hash is not None:
        state.set("user_content_hash", new_hash)
        # "Has a dev session ever spawned" is keyed off the persisted
        # role identity (`dev_agent`, or the legacy `codex_session_id`),
        # NOT off `dev_session_id` alone -- a first spawn whose
        # subprocess returned no session id (CLI hiccup, missing output
        # file) still recorded `dev_agent` and is a valid resume target.
        # `_resume_dev_with_text` handles `dev_sid=None` by spawning
        # fresh against the recorded spec, which is exactly what we
        # want here (the recorded spec also survives a config flip
        # between ticks).
        has_dev_session = bool(
            state.get("dev_agent") or state.get("codex_session_id")
        )
        if has_dev_session:
            _post_issue_comment(
                gh, issue, state,
                ":pencil2: issue body changed; resuming dev session with "
                "the updated requirements.",
            )
            # Mark every issue-thread comment visible right now as
            # consumed: the dev session sees the full conversation via
            # `_recent_comments_text` in the resume prompt, so the next
            # validating->in_review handoff (via
            # `_seed_watermark_past_self`) must NOT replay those comments
            # as fresh PR feedback and re-resume the dev on input it has
            # already handled.
            _mark_drift_comments_consumed(gh, issue, state)
            wt = _worktree_path(spec, issue.number)
            if not wt.exists():
                wt = _ensure_worktree(spec, issue.number)
            # Snapshot HEAD BEFORE the resume so the post-result check
            # below can tell whether THIS resume produced a new commit.
            # `_has_new_commits` only compares against `origin/<base>`,
            # so a recovered worktree carrying pre-existing unpushed
            # commits from a previous tick would mask an empty / failed
            # resume here: an empty dev response would still walk into
            # `_on_commits` and open a PR against commits that never
            # got a chance to address the edited requirements.
            before_sha = _head_sha(wt)
            followup = _build_user_content_change_prompt(
                issue, _recent_comments_text(issue),
            )
            wt, result = _resume_dev_with_text(
                gh, spec, issue, state, followup,
            )
            state.set("last_agent_action_at", _now_iso())
            state.set("branch", _branch_name(issue.number))
            after_sha = _head_sha(wt)
            this_resume_committed = (
                bool(after_sha) and after_sha != before_sha
            )
            if result.timed_out:
                _park_awaiting_human(
                    gh, issue, state,
                    f"{config.HITL_MENTIONS} agent timed out after "
                    f"{config.AGENT_TIMEOUT}s, manual intervention needed.",
                    reason="agent_timeout",
                )
            elif this_resume_committed:
                dirty = _worktree_dirty_files(wt)
                if dirty:
                    _on_dirty_worktree(gh, issue, state, result, dirty)
                else:
                    _on_commits(gh, spec, issue, state, result)
            else:
                # The dev produced no new commit on THIS resume. Accept
                # it as an acknowledgement ONLY when the message ends
                # with the explicit `ACK: <reason>` marker emitted by
                # `_build_user_content_change_prompt`. Any other
                # no-commit response (a real clarification question, an
                # ambiguous comment, or an empty message) falls back to
                # `_on_question` so the issue parks awaiting human --
                # treating a clarification as an ack would post a
                # misleading "existing work satisfies" comment AND
                # leave `awaiting_human=False`, stranding the real
                # question. Recovered pre-existing commits from a prior
                # tick are deliberately NOT pushed here either: the dev
                # must explicitly commit again (or ACK) for the
                # orchestrator to treat the body change as handled.
                ack_reason = _drift_ack_reason(result.last_message or "")
                if ack_reason:
                    quoted = "> " + ack_reason.replace("\n", "\n> ")
                    _post_issue_comment(
                        gh, issue, state,
                        ":speech_balloon: dev session reports the existing "
                        f"work satisfies the edit:\n\n{quoted}",
                    )
                    state.set("silent_park_count", 0)
                else:
                    _on_question(gh, issue, state, result)
            gh.write_pinned_state(issue, state)
            return
        # No dev session yet. If the worktree carries recovered unpushed
        # commits from a previous tick, those commits were authored
        # BEFORE the human edited the issue and no agent has seen the
        # new body. Falling through would let the recovered-worktree
        # shortcut below push them and open a PR against requirements
        # the agent never read. Park awaiting human so the operator
        # decides whether to discard the recovered work and start over
        # or accept it as-is by relabeling. Without this guard, an
        # orchestrator restart between commit and PR open followed by a
        # body edit would silently publish stale work.
        #
        # We rely on `_has_new_commits` alone, not a `Path.exists()`
        # pre-check, because `_has_new_commits` already returns False
        # when the worktree is absent (the underlying `git rev-list`
        # fails) -- and the fake worktree paths used by tests never
        # exist on disk, so an `exists()` gate would short-circuit the
        # park branch in the regression test below.
        wt = _worktree_path(spec, issue.number)
        if _has_new_commits(spec, wt):
            _park_awaiting_human(
                gh, issue, state,
                f"{config.HITL_MENTIONS} issue body changed but the "
                "worktree carries unpushed commits from a previous tick "
                "and no dev session is recorded. Refusing to push commits "
                "that never saw the edited requirements; decide whether "
                "to discard the recovered work (reset the branch) and "
                "let a fresh agent run, or accept it as-is.",
                reason="stale_recovered_work",
            )
            gh.write_pinned_state(issue, state)
            return
        # No recovered commits. If the issue is parked awaiting human
        # WITHOUT a recorded dev session (unusual but possible: a manual
        # relabel, or drift detected on a freshly-picked-up issue that
        # parked before its first spawn), the awaiting-human branch
        # below would route to `_resume_developer_on_human_reply`. Two
        # failure modes there:
        #   (a) no new comments -- returns None and the handler returns
        #       WITHOUT writing the new hash, looping the drift detection
        #       on every subsequent tick;
        #   (b) new comments -- `_resume_dev_with_text` fresh-spawns with
        #       ONLY the new-comments followup text as the prompt, never
        #       quoting the updated body that triggered the drift in the
        #       first place.
        # Clear the park flags here so the fresh-spawn branch below
        # fires this tick with the full implement prompt (which quotes
        # the current `issue.body` and the full conversation via
        # `_recent_comments_text`). Mark every visible issue-thread
        # comment as consumed so the validating->in_review handoff
        # doesn't later replay them as fresh PR feedback.
        if state.get("awaiting_human"):
            _post_issue_comment(
                gh, issue, state,
                ":pencil2: issue content changed; clearing the park and "
                "spawning a fresh dev run against the updated "
                "requirements.",
            )
            _mark_drift_comments_consumed(gh, issue, state)
            state.set("awaiting_human", False)
            state.set("park_reason", None)
        # Fall through to the fresh-spawn path, which builds the
        # implement prompt from the current `issue.body` so the new
        # requirements are picked up naturally.

    if state.get("awaiting_human"):
        resumed = _resume_developer_on_human_reply(gh, spec, issue, state)
        if resumed is None:
            return
        wt, result = resumed
    else:
        wt = _ensure_worktree(spec, issue.number)
        if _has_new_commits(spec, wt):
            # Recovered worktree: the dev agent already committed on a
            # previous tick; skip a fresh run and go straight to push.
            log.info(
                "issue=#%d skipping agent; worktree already has commits",
                issue.number,
            )
            _, _, _, dev_sid = _read_dev_session(state)
            result = AgentResult(
                session_id=dev_sid,
                last_message="(orchestrator restart: pushing previously committed work)",
                exit_code=0,
                timed_out=False,
                stdout="",
                stderr="",
            )
        else:
            if not _check_and_increment_retry_budget(gh, issue, state):
                gh.write_pinned_state(issue, state)
                return
            dev_spec, dev_backend, dev_args, _ = _read_dev_session(state)
            # Persist the spec BEFORE the spawn so a backend hiccup
            # that produces commits without surfacing a session id (an
            # empty codex `-o` file, an unparseable claude JSONL line)
            # does not leave `dev_agent` unset. A later `DEV_AGENT` env
            # flip would otherwise retarget the next resume at a
            # backend that never ran on this issue. Storing the parsed
            # backend alone would also strip any configured CLI args
            # on subsequent resumes. `_read_dev_session` already chose
            # `dev_spec` -- the current stored value when re-entering,
            # else `config.DEV_AGENT_SPEC` for a first-ever spawn --
            # so this is a no-op when state already carries the spec.
            state.set("dev_agent", dev_spec)
            prompt = _build_implement_prompt(issue, _recent_comments_text(issue))
            result = _run_agent_tracked(
                gh, issue.number,
                agent_role="developer",
                stage="implementing",
                backend=dev_backend,
                prompt=prompt,
                cwd=wt,
                extra_args=dev_args,
                retry_count=state.get("retry_count"),
            )
            if result.session_id:
                state.set("dev_session_id", result.session_id)
        state.set("branch", _branch_name(issue.number))

    state.set("last_agent_action_at", _now_iso())

    if result.timed_out:
        # Park on awaiting_human so the next tick doesn't restart codex or
        # push partial commits left in the worktree. The HITL reply acts as
        # the unblock signal, identical to the question path.
        _park_awaiting_human(
            gh, issue, state,
            f"{config.HITL_MENTIONS} agent timed out after {config.AGENT_TIMEOUT}s, "
            "manual intervention needed.",
            reason="agent_timeout",
        )
        gh.write_pinned_state(issue, state)
        return

    wt = _worktree_path(spec, issue.number)
    if _has_new_commits(spec, wt):
        dirty = _worktree_dirty_files(wt)
        if dirty:
            _on_dirty_worktree(gh, issue, state, result, dirty)
        else:
            _on_commits(gh, spec, issue, state, result)
    else:
        _on_question(gh, issue, state, result)

    gh.write_pinned_state(issue, state)


def _handle_dev_fix_result(
    gh: GitHubClient,
    spec: RepoSpec,
    issue: Issue,
    state: PinnedState,
    wt: Path,
    result: AgentResult,
    before_sha: str,
) -> bool:
    """Post-agent handling for a dev fix during validating.

    Returns True if a fix was committed, pushed, and the loop should re-review
    on the next tick. Returns False if the run produced no fix (timeout,
    no-new-commit, dirty tree, or push failure); caller should write state and
    return.
    """
    if result.timed_out:
        _park_awaiting_human(
            gh, issue, state,
            f"{config.HITL_MENTIONS} agent timed out after {config.AGENT_TIMEOUT}s, "
            "manual intervention needed.",
            reason="agent_timeout",
        )
        # Tag as transient: a stuck dev fix-loop (validating CHANGES_REQUESTED
        # or comment-driven resume) clears on the next tick when the validating
        # recovery branch lets the reviewer re-run; the in_review fix-loop
        # leaves it tagged but stays parked because in_review's transient set
        # does not include this reason.
        state.set("park_reason", "agent_timeout")
        # Persist the pre-agent SHA so the recovery branch can tell whether
        # the timeout actually produced a new commit. `_has_new_commits()`
        # would say yes for any normal PR worktree (the dev's earlier fixes
        # are ahead of `origin/<base>` even when this run did nothing), so
        # without this watermark the recovery would force-push a stale
        # local HEAD and bump the round on every tick.
        state.set("pre_dev_fix_sha", before_sha or "")
        return False

    after_sha = _head_sha(wt)
    if after_sha == before_sha or not after_sha:
        # No new commit: dev asked a question or did nothing.
        _on_question(gh, issue, state, result)
        return False

    # A new commit landed -- the session is alive and producing output, so
    # the silent-park streak must reset here. Otherwise a single later
    # empty resume would tip a healthy session past the fresh-session
    # threshold (`silent_park_count` is meant to count *consecutive*
    # silent parks, not lifetime silent parks). Covers the validating /
    # in_review fix paths whose success exit (`return True` below) bypasses
    # `_on_commits` / `_on_dirty_worktree`, which would otherwise be the
    # only resetters on this branch.
    state.set("silent_park_count", 0)

    dirty = _worktree_dirty_files(wt)
    if dirty:
        _on_dirty_worktree(gh, issue, state, result, dirty)
        return False

    branch = _branch_name(issue.number)
    if not _push_branch(spec, wt, branch):
        _park_awaiting_human(
            gh, issue, state,
            f"{config.HITL_MENTIONS} git push failed; see orchestrator logs.",
            reason="push_failed",
        )
        # Tag as transient so a self-resolving condition (the next push
        # succeeds under --force-with-lease once the remote settles) can
        # silently recover the issue without needing a human comment.
        state.set("park_reason", "push_failed")
        return False

    return True


def _post_user_content_change_result(
    gh: GitHubClient,
    spec: RepoSpec,
    issue: Issue,
    state: PinnedState,
    wt: Path,
    result: AgentResult,
    before_sha: str,
) -> str:
    """Post-resume handling for a user-content-change dev resume.

    Returns one of:

    * ``"ack"`` -- the dev produced no commit but explicitly signaled
      acknowledgement via the `ACK: ...` marker emitted by
      `_build_user_content_change_prompt`. The reply is posted on the
      issue as an FYI and the handler does NOT park `awaiting_human`.
      Caller stays on the current label.
    * ``"pushed"`` -- new commit landed and the push succeeded. Caller
      advances the label per its own rules (in_review bounces to
      validating; validating stays with `review_round++`).
    * ``"parked"`` -- timeout, dirty tree, push fail, silent crash
      (empty `last_message`), OR a no-commit response WITHOUT the
      `ACK:` marker (treated as a clarification question via
      `_on_question`). State already carries the park flags.

    The explicit `ACK:` marker is required because a generic non-empty
    no-commit response is often a clarification question, not an
    acknowledgement; swallowing it as an ack would post a misleading
    "existing work satisfies" comment AND continue the workflow with
    `awaiting_human=False`, stranding the real question.
    """
    if result.timed_out:
        _park_awaiting_human(
            gh, issue, state,
            f"{config.HITL_MENTIONS} agent timed out after "
            f"{config.AGENT_TIMEOUT}s, manual intervention needed.",
            reason="agent_timeout",
        )
        state.set("park_reason", "agent_timeout")
        state.set("pre_dev_fix_sha", before_sha or "")
        return "parked"

    after_sha = _head_sha(wt)
    if not after_sha or after_sha == before_sha:
        ack_reason = _drift_ack_reason(result.last_message or "")
        if ack_reason:
            quoted = "> " + ack_reason.replace("\n", "\n> ")
            _post_issue_comment(
                gh, issue, state,
                ":speech_balloon: dev session reports the existing work "
                f"satisfies the edit:\n\n{quoted}",
            )
            # The session is alive and producing output, so the silent-
            # park streak must reset (mirrors `_handle_dev_fix_result`'s
            # reset on a successful commit) -- otherwise a future blip
            # would tip a healthy session past the fresh-session threshold.
            state.set("silent_park_count", 0)
            return "ack"
        # No commit and no explicit ACK marker. The reply may be a real
        # clarification question; falling through to `_on_question`
        # parks the issue awaiting human so a misleading "satisfies"
        # comment isn't posted over a real question. Empty messages
        # land in the same branch and surface as the silent-failure
        # park (`_resume_dev_with_text` uses the streak counter to
        # eventually drop a poisoned session id).
        _on_question(gh, issue, state, result)
        return "parked"

    state.set("silent_park_count", 0)
    dirty = _worktree_dirty_files(wt)
    if dirty:
        _on_dirty_worktree(gh, issue, state, result, dirty)
        return "parked"

    branch = _branch_name(issue.number)
    if not _push_branch(spec, wt, branch):
        _park_awaiting_human(
            gh, issue, state,
            f"{config.HITL_MENTIONS} git push failed; see orchestrator logs.",
            reason="push_failed",
        )
        state.set("park_reason", "push_failed")
        return "parked"

    return "pushed"


def _handle_validating(gh: GitHubClient, spec: RepoSpec, issue: Issue) -> None:
    state = gh.read_pinned_state(issue)
    pr_number = state.get("pr_number")

    # User-content drift: a human edited the issue title/body while the
    # reviewer was running. Re-decomposing now would discard the dev's
    # already-pushed work, so notify the human, resume the dev session on
    # its locked backend with the new body, and on a successful pushed fix
    # stay in `validating` so the reviewer re-runs on the new diff next
    # tick. On a failed resume (timeout, dirty, no commit), the standard
    # park flags land via `_handle_dev_fix_result`.
    #
    # Exception: when the issue is parked with a reviewer-side park reason
    # (`reviewer_timeout` / `reviewer_failed`), defer to the awaiting-human
    # branch below. A human "retry" comment on a reviewer-side park must
    # re-spawn the REVIEWER, not the dev: the failure produced no review
    # output for the dev to act on, and the reviewer naturally re-reads
    # the updated `issue.body` + comments via `_build_review_prompt` when
    # it runs. We still persist the new baseline here so the next tick's
    # drift check sees a stable comparison point (otherwise the drift
    # would loop on every subsequent tick).
    new_hash = _detect_user_content_change(gh, issue, state)
    if new_hash is not None:
        state.set("user_content_hash", new_hash)
        reviewer_side_park = (
            state.get("awaiting_human")
            and state.get("park_reason")
            in ("reviewer_timeout", "reviewer_failed")
        )
        if not reviewer_side_park:
            _post_issue_comment(
                gh, issue, state,
                ":pencil2: issue body changed; resuming dev session.",
            )
            # Mark the full issue thread as consumed: the dev sees it via
            # `_recent_comments_text` in the resume prompt, so the eventual
            # handoff to in_review must not replay those comments as fresh
            # feedback. Mirrors `_resume_developer_on_human_reply`'s
            # pre-spawn bump.
            _mark_drift_comments_consumed(gh, issue, state)
            wt = _worktree_path(spec, issue.number)
            if not wt.exists():
                wt = _ensure_worktree(spec, issue.number)
            before_sha = _head_sha(wt)
            followup = _build_user_content_change_prompt(
                issue, _recent_comments_text(issue),
            )
            wt, result = _resume_dev_with_text(
                gh, spec, issue, state, followup,
            )
            state.set("last_agent_action_at", _now_iso())
            # Custom result handler: a no-commit-with-message reply is the
            # dev confirming the existing work already satisfies the edit,
            # and the resume prompt explicitly invites that response.
            # `_handle_dev_fix_result` would park on it via `_on_question`;
            # use the user-content-specific helper so a harmless clarification
            # does not stall the issue.
            outcome = _post_user_content_change_result(
                gh, spec, issue, state, wt, result, before_sha,
            )
            if outcome == "pushed":
                round_n = int(state.get("review_round") or 0)
                state.set("review_round", round_n + 1)
            gh.write_pinned_state(issue, state)
            return
        # reviewer-side park: fall through to the awaiting_human branch
        # below, which will consume the human's "retry" comment, clear
        # the park flags, and re-spawn the reviewer.

    # Awaiting-human path: human replied after a park; resume the developer
    # codex with their feedback. Identical mechanic to implementing's resume,
    # but on success we stay in validating and bump the round so the reviewer
    # runs again on the next tick.
    if state.get("awaiting_human"):
        # Transient-park recovery: when the original park reason is something
        # that can resolve without a human comment (a push race that the
        # next --force-with-lease push will land, or an agent timeout that
        # the next tick can simply rerun past), re-attempt silently. This
        # mirrors the in_review recovery branch -- without it, the issue
        # would sit forever, because `_resume_developer_on_human_reply`
        # only fires on new issue-thread comments and the human action
        # that unstuck the underlying condition typically does not include
        # one.
        last_action_id = state.get("last_action_comment_id")
        new_comments = gh.comments_after(issue, last_action_id)
        park_reason = state.get("park_reason")
        if (
            not new_comments
            and park_reason in _VALIDATING_TRANSIENT_PARK_REASONS
        ):
            if not _try_recover_validating_transient_park(
                spec, issue, state
            ):
                return  # still stuck, do not re-post the park comment
            # Conditions resolved: clear the park flags. The recovery
            # helper has already bumped review_round when a fix landed
            # (push_failed, or agent_timeout that finished its push), so
            # we only handle the flag clear here. The handoff watermark
            # `last_action_comment_id` was already advanced by the
            # original `_park_awaiting_human` call, so the in_review
            # handler will not re-feed any already-consumed comments.
            state.set("awaiting_human", False)
            state.set("park_reason", None)
            gh.write_pinned_state(issue, state)
            return
        if (
            new_comments
            and park_reason in ("reviewer_timeout", "reviewer_failed")
        ):
            # The park was reviewer-side (timeout or silent crash); a
            # human "Retry" / "Continue" nudge should re-spawn the
            # REVIEWER, not the dev. The dev session has nothing to act
            # on -- the failure produced no review output -- and waking
            # it just yields a "nothing to do" question that re-parks
            # the issue. Advance the watermark past the consumed
            # comments, clear the park flags, and fall through to the
            # reviewer-spawn block below.
            consumed_max = max(c.id for c in new_comments)
            state.set("last_action_comment_id", consumed_max)
            state.set("awaiting_human", False)
            state.set("park_reason", None)
        else:
            wt = _worktree_path(spec, issue.number)
            if not wt.exists():
                wt = _ensure_worktree(spec, issue.number)
            before_sha = _head_sha(wt)
            resumed = _resume_developer_on_human_reply(gh, spec, issue, state)
            if resumed is None:
                return
            wt, result = resumed
            state.set("last_agent_action_at", _now_iso())
            if not _handle_dev_fix_result(
                gh, spec, issue, state, wt, result, before_sha
            ):
                gh.write_pinned_state(issue, state)
                return
            round_n = int(state.get("review_round") or 0)
            state.set("review_round", round_n + 1)
            gh.write_pinned_state(issue, state)
            return

    round_n = int(state.get("review_round") or 0)
    if round_n >= config.MAX_REVIEW_ROUNDS:
        _park_awaiting_human(
            gh, issue, state,
            f"{config.HITL_MENTIONS} review still has comments after "
            f"{round_n} round(s); manual intervention needed.",
            reason="review_cap",
        )
        gh.write_pinned_state(issue, state)
        return

    wt = _ensure_worktree(spec, issue.number)
    # The reviewer reads the local worktree's HEAD; remember which commit
    # that is so the in_review handoff can persist the SHA the agent
    # actually inspected. Setting `agent_approved_sha = pr.head.sha`
    # instead would mark the REMOTE head at handoff time as agent-approved,
    # which lets AUTO_MERGE land an unreviewed commit if the branch was
    # force-pushed or otherwise updated between the review and the handoff.
    reviewed_sha = _head_sha(wt)
    _, dev_backend_for_prompt, _, _ = _read_dev_session(state)
    review_prompt = _build_review_prompt(
        spec, issue, _recent_comments_text(issue), dev_backend_for_prompt,
    )
    # Persist the full configured spec BEFORE the spawn so a reviewer
    # backend hiccup that yields no session id still leaves a durable
    # role-identity record. The trace reflects the reviewer's CLI args
    # and a config flip mid-flight cannot retroactively rewrite which
    # spec ran each round. The reviewer is spawned fresh each round
    # (no resume), so always overwriting the field with the current
    # config spec is the right behavior here.
    state.set("review_agent", config.REVIEW_AGENT_SPEC)
    review = _run_agent_tracked(
        gh, issue.number,
        agent_role="reviewer",
        stage="validating",
        backend=config.REVIEW_AGENT,
        prompt=review_prompt,
        cwd=wt,
        timeout=config.REVIEW_TIMEOUT,
        extra_args=config.REVIEW_AGENT_ARGS,
        review_round=round_n,
        retry_count=state.get("retry_count"),
    )
    if review.session_id:
        state.set("last_review_session_id", review.session_id)
    state.set("last_review_at", _now_iso())

    if review.timed_out:
        _park_awaiting_human(
            gh, issue, state,
            f"{config.HITL_MENTIONS} reviewer timed out after "
            f"{config.REVIEW_TIMEOUT}s; manual intervention needed.",
            reason="reviewer_timeout",
        )
        # Tag as transient so the next tick re-spawns the reviewer instead
        # of waiting for a human comment that the timeout itself does not
        # produce.
        state.set("park_reason", "reviewer_timeout")
        gh.write_pinned_state(issue, state)
        return

    verdict, body = _parse_review_verdict(review.last_message)
    gh.emit_event(
        "review_verdict",
        issue_number=issue.number,
        stage="validating",
        verdict=verdict,
        review_round=round_n,
        pr_number=int(pr_number) if pr_number is not None else None,
        session_id=review.session_id,
    )

    if verdict == "approved":
        if pr_number is not None:
            try:
                _post_pr_comment(
                    gh, int(pr_number), state,
                    f":white_check_mark: {config.REVIEW_AGENT} review approved.",
                )
            except Exception:
                log.exception(
                    "issue=#%s could not post approval to PR #%s",
                    issue.number, pr_number,
                )

        # Squash before seeding the in_review handoff. If the squash or
        # force-push fails we park awaiting_human and STAY in `validating`
        # (no relabel), so the original commits remain on the branch and a
        # human can adjudicate. On success the new local HEAD becomes the
        # SHA AUTO_MERGE will gate on; the existing
        # `agent_approved_sha == pr.head.sha` invariant then keeps holding
        # because the remote also points at the new SHA after the
        # force-push, and `_latest_review_states_for_head` naturally
        # invalidates any stale GitHub review (its `commit_id` no longer
        # matches the new head), leaving the agent's `agent_approved_sha`
        # as the only thing keeping AUTO_MERGE viable -- which is exactly
        # the point.
        new_head_sha = reviewed_sha
        squashed_count = 0
        if config.SQUASH_ON_APPROVAL:
            success, sha_after, n_squashed, err = _squash_and_force_push(
                spec, wt, _branch_name(issue.number), issue,
            )
            if not success:
                _park_awaiting_human(
                    gh, issue, state,
                    f"{config.HITL_MENTIONS} squash-on-approval failed "
                    f"({err}); the original commits are still on the "
                    "branch and the PR was not relabeled. Manual "
                    "intervention needed (squash + force-push by hand, "
                    "or set `SQUASH_ON_APPROVAL=off` and re-run the "
                    "reviewer).",
                    reason="squash_failed",
                )
                gh.write_pinned_state(issue, state)
                return
            if sha_after:
                new_head_sha = sha_after
            squashed_count = n_squashed

        if pr_number is not None:
            # Snapshot what the reviewer agent approved and seed the
            # in_review comment watermark. Without these, `_handle_in_review`
            # would (a) refuse to auto-merge -- the agent posts an issue
            # comment, not a real PR review, so pr_is_approved alone is
            # always False for the agent flow -- and (b) replay the
            # orchestrator's own automated comments ("picking this up",
            # "PR opened", the approval just posted) as fresh PR feedback
            # once the debounce expires.
            try:
                pr = gh.get_pr(int(pr_number))
            except Exception as e:
                # Recoverable: AUTO_MERGE will simply not fire for this
                # issue, and the in_review handler will fall back to its
                # legacy `last_action_comment_id` watermark. Surface the
                # failure but skip the traceback -- it adds no signal.
                log.warning(
                    "issue=#%s could not snapshot PR #%s for in_review "
                    "handoff: %s", issue.number, pr_number, e,
                )
            else:
                # Post the squash PR comment BEFORE seeding watermarks so
                # the seed walks past it (its id lands in
                # `orchestrator_comment_ids` via `_post_pr_comment`).
                # Without that ordering, the next in_review tick treats
                # the squash comment as fresh PR feedback once the
                # debounce expires and resumes the dev session over an
                # informational orchestrator post.
                if squashed_count > 1:
                    try:
                        _post_pr_comment(
                            gh, int(pr_number), state,
                            f":package: squashed {squashed_count} commits "
                            "to 1 after approval",
                        )
                    except Exception:
                        log.exception(
                            "issue=#%s could not post squash notice to "
                            "PR #%s", issue.number, pr_number,
                        )
                # Persist the local SHA the reviewer (or the squash)
                # produced, not the current remote head. The auto-merge
                # gate's existing `agent_approved_sha == head_sha` check
                # then naturally rejects the branch-update race: if the
                # remote moves past this SHA, agent_approved_sha won't
                # match and AUTO_MERGE waits for a fresh review round.
                if new_head_sha:
                    state.set("agent_approved_sha", new_head_sha)
                issue_wm, review_wm = _latest_pr_comment_ids(
                    gh, issue, pr, state
                )
                # Ratchet: a previous in_review tick may have already
                # advanced these watermarks past PR feedback the dev has
                # since fixed. _seed_watermark_past_self stops at the first
                # post-pickup human comment, so without max() that consumed
                # comment would replay as "new" on the next in_review tick.
                prev_issue_wm = state.get("pr_last_comment_id")
                if isinstance(prev_issue_wm, int):
                    issue_wm = (
                        prev_issue_wm if issue_wm is None
                        else max(issue_wm, prev_issue_wm)
                    )
                # Default to 0 ("scan all from the beginning") when the
                # seed-past-self logic returned None and no prior watermark
                # exists. That happens for legacy state without a recorded
                # pickup id; setting 0 stops the in_review legacy migration
                # from then advancing past historical content (including
                # human feedback posted during implementing/validating)
                # while still letting `orchestrator_comment_ids` filter
                # recorded bot comments out of the next tick's scan.
                if issue_wm is None:
                    issue_wm = 0
                state.set("pr_last_comment_id", issue_wm)
                # Inline review comments and review summaries live in
                # namespaces the orchestrator never posts on, so the
                # seed-past-self logic always returns None for those
                # surfaces. Default each to 0 ("scan all from beginning")
                # so the in_review legacy migration sees them as already
                # seeded and does NOT advance past human feedback the
                # human submitted on those surfaces during validate. Ratchet
                # past anything a prior in_review tick already consumed.
                prev_review_wm = state.get("pr_last_review_comment_id")
                if isinstance(prev_review_wm, int):
                    review_wm = (
                        prev_review_wm if review_wm is None
                        else max(review_wm, prev_review_wm)
                    )
                if review_wm is None:
                    review_wm = 0
                state.set("pr_last_review_comment_id", review_wm)
                prev_summary_wm = state.get("pr_last_review_summary_id")
                summary_wm = (
                    prev_summary_wm
                    if isinstance(prev_summary_wm, int)
                    else 0
                )
                state.set("pr_last_review_summary_id", summary_wm)
        gh.set_workflow_label(issue, "in_review")
        gh.write_pinned_state(issue, state)
        return

    if verdict == "unknown":
        raw = (review.last_message or "").strip() or "(reviewer produced no final message)"
        quoted = "> " + raw.replace("\n", "\n> ")
        # Surface stderr only on the silent-review path (empty last_message).
        # If the reviewer DID emit text but it just lacked a VERDICT line,
        # the human is reading real model output and stderr noise would
        # only distract.
        silent_crash = (
            not (review.last_message or "").strip() and review.exit_code != 0
        )
        diag = (
            _format_stderr_diagnostics(review, "Reviewer")
            if not (review.last_message or "").strip()
            else ""
        )
        _park_awaiting_human(
            gh, issue, state,
            f"{config.HITL_MENTIONS} reviewer did not emit a VERDICT line; "
            f"manual adjudication needed.\n\n_Last reviewer message:_\n\n"
            f"{quoted}{diag}",
            reason="reviewer_failed" if silent_crash else "reviewer_no_verdict",
        )
        if silent_crash:
            # Tag as transient so the next tick re-spawns the reviewer
            # instead of waking the dev on a human "Retry" comment.
            # Mirrors `reviewer_timeout`: a crash with empty stdout +
            # non-zero exit (codex-side error, network blip) leaves no
            # output the dev could act on, and `_resume_developer_on_human_reply`
            # would otherwise hand the wrong agent a do-nothing prompt.
            state.set("park_reason", "reviewer_failed")
        log.warning(
            "issue=#%s reviewer emitted no VERDICT; exit_code=%d "
            "timed_out=%s stderr_tail=%r",
            issue.number, review.exit_code, review.timed_out,
            _stderr_log_tail(review),
        )
        gh.write_pinned_state(issue, state)
        return

    # CHANGES_REQUESTED -- post the feedback on the PR, then resume the dev.
    feedback = body.strip() or (review.last_message or "").strip()
    if pr_number is not None:
        try:
            _post_pr_comment(
                gh, int(pr_number), state,
                f":eyes: {config.REVIEW_AGENT} review (round {round_n + 1}/"
                f"{config.MAX_REVIEW_ROUNDS}) requested changes:\n\n{feedback}",
            )
        except Exception:
            log.exception(
                "issue=#%s could not post review to PR #%s",
                issue.number, pr_number,
            )

    fix_prompt = _build_fix_prompt(feedback)
    before_sha = _head_sha(wt)
    _, dev_backend, dev_args, dev_sid = _read_dev_session(state)
    dev_result = _run_agent_tracked(
        gh, issue.number,
        agent_role="developer",
        stage="validating",
        backend=dev_backend,
        prompt=fix_prompt,
        cwd=wt,
        resume_session_id=dev_sid,
        extra_args=dev_args,
        review_round=round_n,
        retry_count=state.get("retry_count"),
    )
    state.set("last_agent_action_at", _now_iso())

    if not _handle_dev_fix_result(
        gh, spec, issue, state, wt, dev_result, before_sha
    ):
        gh.write_pinned_state(issue, state)
        return

    state.set("review_round", round_n + 1)
    gh.write_pinned_state(issue, state)


def _seed_watermark_past_self(
    issue_thread_comments: list,
    pr_conversation_comments: list,
    orchestrator_ids: set[int],
    pickup_comment_id: Optional[int],
    consumed_through: Optional[int] = None,
) -> Optional[int]:
    """Seed the in_review handoff watermark.

    Walk comments oldest-to-newest across both surfaces (issue thread and
    PR conversation share the IssueComment id space, so a single watermark
    covers both). The pickup comment is the boundary: everything before
    `pickup_comment_id` is pre-pickup chatter the dev agent already saw at
    spawn, so it can be advanced past. From the pickup forward, advance
    through the contiguous run of orchestrator-authored comments AND
    through any ISSUE-THREAD comment with id <= `consumed_through` (already
    fed to the dev agent via a prior `_resume_developer_on_human_reply`
    call during implementing/validating), stopping at the first
    not-yet-consumed non-orchestrator comment. This preserves human
    feedback posted during validating that the dev has not yet seen while
    NOT replaying feedback the dev has already consumed.

    `consumed_through` is intentionally NOT applied to PR-conversation
    comments. `last_action_comment_id` only records issue-thread ids fed
    via `_resume_developer_on_human_reply` (validating/implementing watch
    the issue thread only); a PR-conversation comment whose id happens to
    be <= a later-consumed issue-thread reply has NOT been seen by the dev
    and must surface on the next in_review tick. Folding both surfaces
    under one `c.id <= consumed_through` check would let AUTO_MERGE land
    the PR over unread PR-conversation feedback.

    Identification of orchestrator-authored content is by exact comment id
    (recorded when the orchestrator posted the comment) rather than author
    login. The login-based check would also drop comments authored by a
    human reviewer who shares the PAT's GitHub account -- a common
    deployment shape -- causing real review feedback to be auto-merged over.

    Returns None when the pickup id is unknown (legacy state from a deploy
    that pre-dates pickup-id tracking, or a manually-relabeled issue) or
    when the surface has no orchestrator-authored content. The caller then
    defaults the watermark to 0 so the in_review legacy migration cannot
    advance past historical content; the orchestrator_comment_ids id-set
    filter in `_handle_in_review` drops recorded bot comments at scan time.
    """
    if pickup_comment_id is None:
        # Legacy state without a pickup anchor: refuse to advance. We
        # cannot tell pre-pickup chatter (safe to skip) from human feedback
        # posted during implementing/validating (must preserve), and
        # dropping a human comment is the unsafe direction.
        return None
    # Tag each comment with its surface so the walk below can apply
    # `consumed_through` to the issue thread only.
    sorted_pairs: list[Tuple[Any, bool]] = sorted(
        [(c, True) for c in issue_thread_comments]
        + [(c, False) for c in pr_conversation_comments],
        key=lambda p: p[0].id,
    )
    if not any(c.id in orchestrator_ids for c, _ in sorted_pairs):
        return None
    watermark: Optional[int] = None
    seen_self = False
    for c, is_issue_thread in sorted_pairs:
        is_self = c.id in orchestrator_ids
        already_consumed = (
            is_issue_thread
            and consumed_through is not None
            and c.id <= consumed_through
        )
        if is_self:
            watermark = c.id
            seen_self = True
        elif not seen_self and c.id < pickup_comment_id:
            # Pre-pickup chatter -- already in the dev agent's spawn context.
            watermark = c.id
        elif already_consumed:
            # Fed to the dev via a prior implementing/validating resume.
            # Replaying it as fresh PR feedback would re-spawn the dev on
            # input it has already handled.
            watermark = c.id
        else:
            # Post-pickup human comment that has NOT been consumed yet.
            # Stop and preserve for the next in_review tick.
            break
    return watermark


def _latest_pr_comment_ids(
    gh: GitHubClient, issue: Issue, pr, state: PinnedState
) -> Tuple[Optional[int], Optional[int]]:
    """Return (issue-comment watermark, review-comment watermark) seeded only
    past leading orchestrator-authored comments on the issue thread + PR.

    The second value is always None: the orchestrator never posts inline PR
    review comments, so there is no leading self-run to advance past on
    that surface, and `orchestrator_comment_ids` records IDs in the
    IssueComment namespace only -- feeding it to `_seed_watermark_past_self`
    against the PullRequestComment namespace would falsely treat a human
    inline comment whose numeric id collides with a recorded bot id as
    self-authored, advancing the watermark past the human's feedback. The
    `_handle_validating` caller defaults the inline-review watermark to 0
    when this returns None so the in_review legacy migration cannot then
    advance past human inline feedback either.
    """
    orchestrator_ids = _orchestrator_ids(state)
    pickup_id_raw = state.get("pickup_comment_id")
    pickup_id = pickup_id_raw if isinstance(pickup_id_raw, int) else None
    # `last_action_comment_id` doubles as a "consumed through" marker:
    # both park comments and post-resume bumps land here, so any issue
    # comment with id <= this value has either been posted by the
    # orchestrator (filtered by `orchestrator_comment_ids`) or already
    # been fed to the dev session (must not replay).
    consumed_raw = state.get("last_action_comment_id")
    consumed_through = (
        consumed_raw if isinstance(consumed_raw, int) else None
    )
    # Keep the surfaces separate -- `consumed_through` only applies to the
    # issue thread (the surface `_resume_developer_on_human_reply` watches
    # during implementing/validating). Folding both into one list and
    # applying `c.id <= consumed_through` uniformly would silently advance
    # the watermark past unread PR-conversation feedback whose id happens
    # to be lower than a later-consumed issue-thread reply, letting
    # AUTO_MERGE land the PR over the human's PR comment.
    issue_thread = list(gh.comments_after(issue, None))
    pr_conversation = list(gh.pr_conversation_comments_after(pr, None))
    return (
        _seed_watermark_past_self(
            issue_thread, pr_conversation,
            orchestrator_ids, pickup_id,
            consumed_through=consumed_through,
        ),
        None,
    )


def _bump_in_review_watermarks(
    gh: GitHubClient,
    issue: Issue,
    state: PinnedState,
    *,
    issue_space_new: Optional[list] = None,
    review_space_new: Optional[list] = None,
    review_summary_new: Optional[list] = None,
) -> None:
    """Push the in_review watermarks past anything we've seen so far AND past
    any park comment we just wrote on the issue thread.

    Without this, a park-and-write at in_review (failed checks, unmergeable,
    failed dev fix) leaves `pr_last_comment_id` lagging behind the orchestrator
    park message it just posted; the next tick scans the issue thread from the
    older watermark and resumes the dev agent on the orchestrator's own HITL
    ping. The ratchet is one-way (only ever increases) so callers can pass
    just-consumed comments or omit them and let `latest_comment_id` carry it.
    """
    candidates: list[int] = []
    cur_issue_wm = state.get("pr_last_comment_id")
    if isinstance(cur_issue_wm, int):
        candidates.append(cur_issue_wm)
    last_action = state.get("last_action_comment_id")
    if isinstance(last_action, int):
        candidates.append(last_action)
    latest = gh.latest_comment_id(issue)
    if isinstance(latest, int):
        candidates.append(latest)
    if issue_space_new:
        candidates.extend(c.id for c in issue_space_new)
    if candidates:
        state.set("pr_last_comment_id", max(candidates))

    review_candidates: list[int] = []
    cur_review_wm = state.get("pr_last_review_comment_id")
    if isinstance(cur_review_wm, int):
        review_candidates.append(cur_review_wm)
    if review_space_new:
        review_candidates.extend(c.id for c in review_space_new)
    if review_candidates:
        state.set("pr_last_review_comment_id", max(review_candidates))

    summary_candidates: list[int] = []
    cur_summary_wm = state.get("pr_last_review_summary_id")
    if isinstance(cur_summary_wm, int):
        summary_candidates.append(cur_summary_wm)
    if review_summary_new:
        summary_candidates.extend(r.id for r in review_summary_new)
    if summary_candidates:
        state.set("pr_last_review_summary_id", max(summary_candidates))


def _comment_created_at(comment) -> Optional[datetime]:
    """Return a tz-aware UTC datetime for a comment, or None if unavailable.

    Real PyGithub `IssueComment.created_at` is always set, but the fakes used
    in tests can leave it None when the test doesn't care about debounce.
    PullRequestReview surfaces its timestamp as `submitted_at` rather than
    `created_at`, so the in_review debounce reads either. Naive datetimes are
    interpreted as UTC (PyGithub returns naive UTC).
    """
    ca = getattr(comment, "created_at", None)
    if ca is None:
        ca = getattr(comment, "submitted_at", None)
    if ca is None:
        return None
    if ca.tzinfo is None:
        return ca.replace(tzinfo=timezone.utc)
    return ca


# Park reasons that auto-resolve when the underlying GitHub state changes
# (CI rerun goes green, rebase resolves a conflict, branch protection drops
# a stale required review). Other parks (`missing_pr_number`, dev-fix
# failures) need explicit human action to unstick.
_TRANSIENT_PARK_REASONS = frozenset({"failed_checks", "unmergeable"})

# Validating-side counterpart: park reasons whose underlying condition can
# resolve without any human comment. Without this, a transient validating
# failure would leave the issue parked forever -- `_resume_developer_on_human_reply`
# only fires on a new issue-thread comment, and the human action that
# unstuck the underlying condition (a flake clears, CI settles, the remote
# accepts the next push) typically does not include one.
#
#   `push_failed`     - non-fast-forward push; retried under --force-with-lease.
#   `agent_timeout`   - dev-fix agent timed out; let the next tick re-run the
#                       reviewer (which will spawn the dev again if changes
#                       are still requested).
#   `reviewer_timeout`- reviewer agent timed out; let the next tick re-run it.
#
# Reasons that need human content (a question, a dirty worktree, a verdict
# the agent could not produce) stay parked until a comment arrives.
_VALIDATING_TRANSIENT_PARK_REASONS = frozenset(
    {"push_failed", "agent_timeout", "reviewer_timeout", "reviewer_failed"}
)


def _try_recover_validating_transient_park(
    spec: RepoSpec, issue: Issue, state: PinnedState
) -> bool:
    """Quietly attempt to clear a transient validating park.

    Returns True if the underlying condition has resolved (caller should
    clear the park flags and progress); False to stay parked. Must not
    spawn the agent or post issue/PR comments -- the caller owns the
    visible side of the recovery so a still-stuck tick produces no churn.

    The helper IS allowed to update review-round bookkeeping when a fix
    landed during recovery (e.g. an agent_timeout where the dev had
    actually committed before timing out, and we finish the push here).
    Callers should not mutate the round themselves; this is the only
    write path while the park flags are still set.
    """
    park_reason = state.get("park_reason")
    if park_reason == "push_failed":
        wt = _worktree_path(spec, issue.number)
        if not wt.exists():
            # Worktree was reaped; the dev's local commits are gone, so
            # there is nothing to push. A human has to intervene (relabel
            # back to implementing) -- that's the unblocking signal.
            return False
        if not _push_branch(spec, wt, _branch_name(issue.number)):
            return False
        # The dev's fix is now landed; bump the round so the cap reflects
        # the completed fix cycle.
        round_n = int(state.get("review_round") or 0)
        state.set("review_round", round_n + 1)
        return True
    if park_reason in ("reviewer_timeout", "reviewer_failed"):
        # Reviewer agent only reads the worktree; nothing to reconcile
        # locally. Clear flags so the next tick re-spawns the reviewer
        # with a fresh budget. `reviewer_failed` (silent crash with
        # empty stdout + non-zero exit) self-heals the same way as
        # `reviewer_timeout`: there is no dev-side state to reconcile,
        # and the next tick simply spawns a fresh reviewer.
        return True
    if park_reason == "agent_timeout":
        # The dev agent could have committed or left uncommitted edits
        # before the timeout killed it. Recovery cannot just clear flags
        # -- the next tick's reviewer would inspect the LOCAL worktree
        # and could approve a SHA that is not on the PR, seeding
        # `agent_approved_sha` to an unpushed commit and stalling
        # in_review. Reconcile the worktree explicitly here.
        wt = _worktree_path(spec, issue.number)
        if not wt.exists():
            return False
        if _worktree_dirty_files(wt):
            # The dev left edits that were never committed. We cannot
            # safely push, review, or auto-merge in this state; stay
            # parked until a human or a fresh comment-driven resume
            # sorts it out. A reviewer that ignored the dirty index
            # would vote on the committed head while the leftover edits
            # are silently dropped on the next push.
            return False
        # The pre-agent SHA was persisted when the timeout park ran.
        # Compare against the current worktree HEAD instead of
        # `_has_new_commits()`, which only checks against
        # `origin/<base>` and would always say "yes" for a PR worktree
        # whose earlier fixes already shipped.
        pre_sha = state.get("pre_dev_fix_sha")
        if not isinstance(pre_sha, str):
            # Defensive: the timeout-tagging path always persists this,
            # so a missing watermark means foreign state we cannot
            # reason about. Stay parked rather than risk force-pushing
            # an out-of-date HEAD over the remote.
            return False
        now_sha = _head_sha(wt)
        if not now_sha or now_sha == pre_sha:
            # The timeout produced no new commit. Clear flags but do
            # not bump the round or push -- nothing landed.
            state.set("pre_dev_fix_sha", None)
            return True
        # The dev committed before timing out. Finish what it started
        # by pushing the new SHA; on success the fix is now landed and
        # we bump the round just like the push_failed branch.
        if not _push_branch(spec, wt, _branch_name(issue.number)):
            return False
        state.set("pre_dev_fix_sha", None)
        round_n = int(state.get("review_round") or 0)
        state.set("review_round", round_n + 1)
        return True
    return False


def _auto_merge_gates_pass(
    gh: GitHubClient, pr, state: PinnedState
) -> bool:
    """All conditions required for auto-merge, evaluated quietly (no parking,
    no PR comments, no state writes).

    Used by the transient-park recovery path: when an awaiting_human issue
    re-enters `_handle_in_review` with no new comments, we want to detect a
    silently-resolved condition (CI now green, rebase made the PR mergeable)
    and unstick the merge without re-posting the park message every tick.
    Mirrors the inline gate sequence in `_handle_in_review` exactly so the
    two cannot drift.
    """
    head_sha = pr.head.sha
    if gh.pr_has_changes_requested(pr, head_sha=head_sha):
        return False
    approved_for_head = (
        state.get("agent_approved_sha") == head_sha
        or gh.pr_is_approved(pr, head_sha=head_sha)
    )
    if not approved_for_head:
        return False
    mergeable = gh.pr_is_mergeable(pr)
    if mergeable is None or not mergeable:
        return False
    return gh.pr_combined_check_state(pr) == "success"


def _seed_legacy_in_review_watermarks(
    gh: GitHubClient, issue: Issue, pr, state: PinnedState
) -> None:
    """First-tick migration: seed any missing in_review watermark past every
    comment currently visible on its surface, and record the seed in pinned
    state immediately.

    Issues that reached `in_review` before the validating handoff started
    seeding watermarks (or that were manually relabeled, or whose handoff
    failed to snapshot the PR) sit on `_handle_in_review` with
    `pr_last_comment_id`/`pr_last_review_comment_id`/`pr_last_review_summary_id`
    all unset. Without this seed, the next tick would call
    `comments_after(..., None)` on each surface and treat every historical
    comment -- including the orchestrator's own pickup / PR-opened / approval
    messages -- as fresh PR feedback once the debounce expires, resuming the
    dev and bouncing the PR back to validating even with `AUTO_MERGE` off.

    Tests that want to drive `_handle_in_review` against pre-existing comments
    seed the relevant watermark explicitly so this helper is a no-op for them.
    """
    # Each missing watermark is persisted on this tick -- 0 if the surface
    # currently has no content, otherwise the latest visible id. Persisting
    # 0 in the empty case is what stops the migration from re-firing on the
    # next tick: if we left the watermark unset, the FIRST human inline /
    # summary review added afterward would be consumed by a re-run of this
    # seed before `_handle_in_review` builds `new_comments`, so AUTO_MERGE
    # could land the PR over that first review.
    seeded = False
    if (
        state.get("pr_last_comment_id") is None
        and state.get("last_action_comment_id") is None
    ):
        candidates: list[int] = []
        issue_latest = gh.latest_comment_id(issue)
        if isinstance(issue_latest, int):
            candidates.append(issue_latest)
        pr_conv = list(gh.pr_conversation_comments_after(pr, None))
        if pr_conv:
            candidates.append(max(c.id for c in pr_conv))
        state.set("pr_last_comment_id", max(candidates) if candidates else 0)
        seeded = True

    if state.get("pr_last_review_comment_id") is None:
        inline = list(gh.pr_inline_comments_after(pr, None))
        state.set(
            "pr_last_review_comment_id",
            max(c.id for c in inline) if inline else 0,
        )
        seeded = True

    if state.get("pr_last_review_summary_id") is None:
        summaries = list(gh.pr_reviews_after(pr, None))
        state.set(
            "pr_last_review_summary_id",
            max(r.id for r in summaries) if summaries else 0,
        )
        seeded = True

    if seeded:
        gh.write_pinned_state(issue, state)


def _handle_in_review(gh: GitHubClient, spec: RepoSpec, issue: Issue) -> None:
    """Drive an in_review issue toward done / rejected, or back to validating
    on a new PR comment.

    The handler always re-checks PR state (merged/closed) first so an external
    human merge wins over any orchestrator-side logic. A PR comment newer than
    the debounce window resumes the dev's locked-backend session and bounces
    the issue back to `validating` so the reviewer agent re-runs on the fix.
    Auto-merge is gated by `AUTO_MERGE` (default off); without it, the loop
    only handles state transitions and comment-driven re-fixes -- humans still
    click Merge.
    """
    state = gh.read_pinned_state(issue)
    pr_number = state.get("pr_number")

    if pr_number is None:
        # Manual relabel from outside the validating path. We don't try to
        # infer the PR -- park once and let the human relabel back.
        if state.get("awaiting_human"):
            return
        _park_awaiting_human(
            gh, issue, state,
            f"{config.HITL_MENTIONS} `in_review` without a pinned `pr_number`; "
            "manual relabeling suspected. Set the workflow label back to "
            "`validating` (or `implementing`) after fixing.",
            reason="missing_pr_number",
        )
        gh.write_pinned_state(issue, state)
        return

    pr = gh.get_pr(int(pr_number))
    pr_status = gh.pr_state(pr)

    if pr_status == "merged":
        state.set("merged_at", _now_iso())
        gh.set_workflow_label(issue, "done")
        gh.write_pinned_state(issue, state)
        gh.emit_event(
            "pr_merged",
            issue_number=issue.number,
            stage="in_review",
            pr_number=int(pr_number),
            sha=getattr(pr.head, "sha", None) or None,
            merge_method="external",
            review_round=int(state.get("review_round") or 0),
            conflict_round=state.get("conflict_round"),
            retry_count=state.get("retry_count"),
        )
        try:
            issue.edit(state="closed")
        except Exception:
            log.exception(
                "issue=#%s could not close after merge", issue.number,
            )
        _cleanup_terminal_branch(gh, spec, issue.number)
        return

    if pr_status == "closed":  # closed without merge
        state.set("closed_without_merge_at", _now_iso())
        gh.set_workflow_label(issue, "rejected")
        gh.write_pinned_state(issue, state)
        gh.emit_event(
            "pr_closed_without_merge",
            issue_number=issue.number,
            stage="in_review",
            pr_number=int(pr_number),
            sha=getattr(pr.head, "sha", None) or None,
            review_round=int(state.get("review_round") or 0),
            conflict_round=state.get("conflict_round"),
            retry_count=state.get("retry_count"),
        )
        try:
            issue.edit(state="closed")
        except Exception:
            log.exception(
                "issue=#%s could not close after reject", issue.number,
            )
        # The PR is gone, so the orchestrator-owned branch and worktree
        # are dead weight. Mirrors the merged-PR cleanup order: finalize
        # GitHub state first, then tidy local + remote refs best-effort.
        _cleanup_terminal_branch(gh, spec, issue.number)
        return

    # PR is open BUT the issue was closed manually (the closed-in_review sweep
    # in `list_pollable_issues` yielded it). Closing the issue while its PR
    # is still open is a human stop signal -- without this branch, AUTO_MERGE
    # could otherwise land the PR and flip the issue to `done` over the
    # human's rejection. The closed-with-merged-PR path (Resolves #N
    # auto-close) is already handled by the `pr_status == "merged"` branch
    # above, so by the time we reach here a closed issue means the human
    # closed it directly.
    #
    # Deliberately NOT cleaning the branch here: the PR is still open and
    # the operator may want to inspect, salvage commits, transfer, or
    # reopen it. Deleting the branch would make the PR harder to review.
    #
    # Automatic cleanup-on-PR-close only happens if the PR is closed
    # BEFORE this handler flips the issue to `rejected`. Once the label
    # is `rejected` the dispatcher (workflow.py terminal-label branch)
    # is a no-op AND `list_pollable_issues` only sweeps closed issues
    # still labeled `in_review` / `resolving_conflict`, so a later PR
    # close is never observed by the orchestrator. The operator must
    # clean up the worktree, local branch, and remote branch manually
    # for the "close issue first, then close PR" ordering.
    if getattr(issue, "state", "open") == "closed":
        state.set("closed_without_merge_at", _now_iso())
        gh.set_workflow_label(issue, "rejected")
        gh.write_pinned_state(issue, state)
        # Deliberately no `pr_closed_without_merge` emit here: the PR is
        # still open and may be reopened / salvaged. That event is reserved
        # for the actual closed-PR rejection arc above.
        return

    # User-content drift: a human edited the issue title/body after the PR
    # opened. Notify on both surfaces, resume the dev session on its locked
    # backend with the new body, and on a successful pushed fix bounce back
    # to `validating` so the reviewer agent re-runs on the new diff next
    # tick (mirrors the comment-driven dev-resume path below).
    new_hash = _detect_user_content_change(gh, issue, state)
    if new_hash is not None:
        state.set("user_content_hash", new_hash)
        # Gather unread PR-conversation comments BEFORE posting the
        # orchestrator's drift notice. The issue thread and PR
        # conversation share the IssueComment id space, so the
        # subsequent `_bump_in_review_watermarks` bump (driven by
        # `latest_comment_id(issue)` and `last_action_comment_id`,
        # both of which only reflect the ISSUE thread) can leap past
        # a PR-conversation comment whose id falls between the prior
        # `pr_last_comment_id` and the new issue-thread max -- the
        # dev would never see it. Capturing those PR comments here
        # and quoting them in the followup prompt is what stops a
        # concurrent PR comment from being silently dropped by the
        # watermark bump. Filtering by `orchestrator_comment_ids` and
        # `_ORCH_COMMENT_MARKER` mirrors the regular in_review
        # comment-driven scan; a PR comment whose id is recorded as
        # orchestrator-authored or whose body carries the marker is
        # not real feedback and stays out of the followup.
        issue_wm = state.get("pr_last_comment_id")
        if issue_wm is None:
            issue_wm = state.get("last_action_comment_id")
        orchestrator_ids = _orchestrator_ids(state)
        unread_pr_conv = [
            c for c in gh.pr_conversation_comments_after(pr, issue_wm)
            if c.id not in orchestrator_ids
            and _ORCH_COMMENT_MARKER not in (c.body or "")
        ]
        _post_pr_comment(
            gh, int(pr_number), state,
            ":pencil2: issue body changed; resuming dev session.",
        )
        # Mark every issue-thread comment as consumed AND bump the
        # in_review watermarks past anything posted on this tick. The
        # dev sees the full thread via `_recent_comments_text` in the
        # resume prompt, so a later validating->in_review handoff (after
        # the "pushed" branch flips to validating and the reviewer
        # approves) and the in_review's own watermark check must not
        # replay these comments as fresh feedback.
        _mark_drift_comments_consumed(gh, issue, state)
        wt = _worktree_path(spec, issue.number)
        if not wt.exists():
            wt = _ensure_worktree(spec, issue.number)
        before_sha = _head_sha(wt)
        # Combine the issue-thread context with the unread PR-conversation
        # comments so the dev sees both surfaces before the watermark
        # bump below consumes them.
        comments_text = _recent_comments_text(issue)
        if unread_pr_conv:
            pr_block = "\n\n".join(
                f"@{c.user.login if c.user else 'user'} (PR comment): "
                f"{c.body or ''}"
                for c in unread_pr_conv
            )
            prefix = f"{comments_text}\n\n" if comments_text else ""
            comments_text = (
                f"{prefix}Unread PR conversation comments:\n\n{pr_block}"
            )
        followup = _build_user_content_change_prompt(issue, comments_text)
        wt, dev_result = _resume_dev_with_text(
            gh, spec, issue, state, followup,
        )
        state.set("last_agent_action_at", _now_iso())
        # The user-content-change result handler treats a no-commit reply
        # as an ack rather than parking on it; a harmless clarification
        # edit (the dev confirms the PR already satisfies it) must not
        # stall the issue with an "agent needs your input" park.
        outcome = _post_user_content_change_result(
            gh, spec, issue, state, wt, dev_result, before_sha,
        )
        # Always bump in_review watermarks past the orchestrator's notice
        # and any comments we just consumed, regardless of outcome. On
        # "pushed" the next tick will be in validating, but if the
        # reviewer later approves and bounces back to in_review,
        # `_seed_watermark_past_self` would otherwise stop at the
        # original human comment and trigger a duplicate resume. Passing
        # `unread_pr_conv` ensures PR-conversation ids ABOVE the
        # issue-thread max are also included in the candidate set;
        # without it, a PR comment with id higher than every issue-thread
        # id would survive past the bump and re-fire as fresh feedback.
        _bump_in_review_watermarks(
            gh, issue, state, issue_space_new=unread_pr_conv,
        )
        if outcome in ("pushed", "ack"):
            # The drift invalidated the prior validation: the reviewer
            # agent approved against the OLD requirements, so its
            # `agent_approved_sha` snapshot is stale. Bounce back to
            # `validating` (reset `review_round=0`) so the reviewer
            # re-evaluates against the updated body/comments before
            # AUTO_MERGE is allowed to land the PR.
            #
            # An "ack" outcome (no commit; dev said the existing work
            # already satisfies the edit) MUST also bounce: leaving the
            # issue at `in_review` with the old `agent_approved_sha`
            # would let the AUTO_MERGE gate pass on the next tick even
            # though the reviewer never saw the changed requirements.
            # Clear `agent_approved_sha` defensively in case any future
            # auto-merge path reads it before the reviewer re-snapshots.
            state.set("review_round", 0)
            state.set("agent_approved_sha", None)
            gh.set_workflow_label(issue, "validating")
        gh.write_pinned_state(issue, state)
        return

    # PR is open. Look for new human activity. Three watermarks because the
    # three comment surfaces live in distinct id namespaces in GitHub's REST
    # API: issue/PR-conversation comments share the IssueComment id space,
    # inline review comments live in the PullRequestComment id space, and
    # PR review summaries (the body posted alongside an APPROVE / REQUEST
    # CHANGES / COMMENT submission) live in the PullRequestReview id space.
    # Mixing any two under one int would silently drop or replay one side.
    # Orchestrator-authored items are filtered by exact id (recorded when
    # we posted them); we cannot key this on author login because a PAT
    # shared with a human reviewer's GitHub account is a normal deployment
    # shape, and login-matching would silently drop that human's feedback.
    # The id-set filter is restricted to the IssueComment namespace -- the
    # only surface the orchestrator posts on -- so a human inline review
    # comment or PR review summary that happens to share a numeric id with
    # a recorded bot comment is not falsely dropped.
    _seed_legacy_in_review_watermarks(gh, issue, pr, state)
    # `or` would discard a legacy default of `pr_last_comment_id == 0` and
    # fall back to `last_action_comment_id` (the id of a prior park
    # comment), which sits ABOVE any human "do not merge yet" comment
    # posted earlier during implementing/validating; that human comment
    # would then never surface and AUTO_MERGE could land the PR over it.
    # Treat 0 as a valid "scan from the beginning" watermark.
    issue_wm = state.get("pr_last_comment_id")
    if issue_wm is None:
        issue_wm = state.get("last_action_comment_id")
    review_wm = state.get("pr_last_review_comment_id")
    review_summary_wm = state.get("pr_last_review_summary_id")
    orchestrator_ids = _orchestrator_ids(state)
    new_issue_side = [
        c for c in gh.comments_after(issue, issue_wm)
        if c.id not in orchestrator_ids
    ]
    new_pr_conv = [
        c for c in gh.pr_conversation_comments_after(pr, issue_wm)
        if c.id not in orchestrator_ids
    ]
    new_pr_inline = list(gh.pr_inline_comments_after(pr, review_wm))
    new_pr_reviews = list(gh.pr_reviews_after(pr, review_summary_wm))
    issue_space_new = sorted(
        list(new_issue_side) + list(new_pr_conv), key=lambda c: c.id
    )
    review_space_new = sorted(new_pr_inline, key=lambda c: c.id)
    review_summary_new = sorted(new_pr_reviews, key=lambda r: r.id)
    new_comments = issue_space_new + review_space_new + review_summary_new

    # If a previous tick already parked on an unrecoverable state and
    # nothing changed since, do nothing -- the human action that unsticks
    # us is a comment, a relabel, or closing/merging the PR. The first two
    # land in `new_comments`; the last two are caught by the `pr_status`
    # branches above.
    #
    # Exception: when the park reason is transient (failed checks or PR not
    # yet mergeable) and `AUTO_MERGE` is on, re-evaluate the gates here. A
    # human who reruns CI green or rebases the branch without leaving a
    # comment would otherwise leave the issue stuck in_review forever.
    if state.get("awaiting_human") and not new_comments:
        if not (
            config.AUTO_MERGE
            and state.get("park_reason") in _TRANSIENT_PARK_REASONS
        ):
            return
        if not _auto_merge_gates_pass(gh, pr, state):
            return  # still stuck, do not re-post the park comment
        # Conditions resolved: clear the park flags and fall through to the
        # auto-merge block, which re-checks the same gates and merges.
        state.set("awaiting_human", False)
        state.set("park_reason", None)

    if new_comments:
        timestamps = [
            ts for ts in (_comment_created_at(c) for c in new_comments)
            if ts is not None
        ]
        if timestamps:
            newest_ts = max(timestamps)
            elapsed = (datetime.now(timezone.utc) - newest_ts).total_seconds()
            if elapsed < config.IN_REVIEW_DEBOUNCE_SECONDS:
                return  # human may still be typing; wait a tick

        followup = _build_pr_comment_followup(new_comments)
        wt = _worktree_path(spec, issue.number)
        if not wt.exists():
            wt = _ensure_worktree(spec, issue.number)
        before_sha = _head_sha(wt)
        wt, dev_result = _resume_dev_with_text(gh, spec, issue, state, followup)
        state.set("last_agent_action_at", _now_iso())
        if not _handle_dev_fix_result(
            gh, spec, issue, state, wt, dev_result, before_sha
        ):
            # Park has updated last_action_comment_id; bump the in_review
            # watermarks past anything we just consumed so the next tick does
            # not replay these comments OR the orchestrator's own park
            # message as fresh PR feedback.
            _bump_in_review_watermarks(
                gh, issue, state,
                issue_space_new=issue_space_new,
                review_space_new=review_space_new,
                review_summary_new=review_summary_new,
            )
            gh.write_pinned_state(issue, state)
            return
        # Successful fix pushed -- bounce back to validating so the reviewer
        # re-runs on the next tick. Reset round counter; this is a new diff.
        if issue_space_new:
            state.set(
                "pr_last_comment_id", max(c.id for c in issue_space_new)
            )
        if review_space_new:
            state.set(
                "pr_last_review_comment_id",
                max(c.id for c in review_space_new),
            )
        if review_summary_new:
            state.set(
                "pr_last_review_summary_id",
                max(r.id for r in review_summary_new),
            )
        state.set("review_round", 0)
        gh.set_workflow_label(issue, "validating")
        gh.write_pinned_state(issue, state)
        return

    if issue_has_label(issue, BASE_SYNC_HOLD_LABEL):
        log.info(
            "issue=#%d has %r; holding in_review auto-merge/base-sync gates",
            issue.number, BASE_SYNC_HOLD_LABEL,
        )
        return

    # No new comments. The two AUTO_MERGE branches diverge on what
    # "unmergeable" means:
    #
    #   * AUTO_MERGE off: legacy fallback path. Humans drive the merge,
    #     so an unmergeable PR just needs visibility -- park awaiting
    #     human regardless of approval state. Unauthenticated humans
    #     re-approve as part of fixing the unmergeable state, so gating
    #     the legacy park on approval would silently hide the unmergeable
    #     condition for any PR that hasn't been re-approved yet.
    #
    #   * AUTO_MERGE on: the resolving_conflict route REPLACES the old
    #     unmergeable park, which only fired AFTER the changes-requested
    #     and approval gates. Resuming dev work for an unapproved PR (or
    #     one carrying a standing human CHANGES_REQUESTED) would push
    #     unreviewed work past a human veto, so the gates run first and
    #     the unmergeable check is gated behind them.
    if not config.AUTO_MERGE:
        mergeable = gh.pr_is_mergeable(pr)
        if mergeable is None:
            return  # GitHub still computing; try next tick
        if not mergeable:
            _park_awaiting_human(
                gh, issue, state,
                f"{config.HITL_MENTIONS} PR #{pr_number} is not mergeable "
                "(branch protection, conflicts, or out-of-date base); "
                "manual merge needed.",
                reason="unmergeable",
            )
            state.set("park_reason", "unmergeable")
            _bump_in_review_watermarks(gh, issue, state)
            gh.write_pinned_state(issue, state)
            return
        return  # mergeable: humans drive the merge from here

    # AUTO_MERGE on. Run the original gating order: changes-requested
    # and approval first, then mergeable. This matches the pre-rollout
    # behavior and ensures the resolving_conflict route only fires for
    # PRs that would have hit the old unmergeable park.
    head_sha = pr.head.sha
    # A human CHANGES_REQUESTED on the current head vetoes auto-merge
    # regardless of how the reviewer agent voted. Without this check, the
    # `agent_approved_sha == head_sha` short-circuit below would let the
    # orchestrator merge over a standing human objection on the same SHA.
    if gh.pr_has_changes_requested(pr, head_sha=head_sha):
        return
    # Approval can come from either side: the reviewer agent persists
    # `agent_approved_sha` for the head it OK'd (the agent posts an issue
    # comment, not a real PR review, so pr_is_approved alone would never be
    # True for the agent flow), OR a human/bot submitted a real APPROVED
    # review on the *current* head SHA. Stale human approvals on older
    # commits do NOT count -- a commit pushed after a human approval must
    # not auto-merge unless the human re-approves.
    approved_for_head = (
        state.get("agent_approved_sha") == head_sha
        or gh.pr_is_approved(pr, head_sha=head_sha)
    )
    if not approved_for_head:
        return
    mergeable = gh.pr_is_mergeable(pr)
    if mergeable is None:
        return  # GitHub still computing; try next tick
    if pr.head.sha != head_sha:
        # `pr_is_mergeable` calls `pr.update()` to resolve a `None`
        # mergeable, which refreshes `pr.head.sha`. The approval and
        # changes-requested gates above ran against the earlier head_sha,
        # so a commit landing during the refresh would otherwise let the
        # subsequent failed-checks branch park on the WRONG sha or, worse,
        # let an unreviewed head reach the merge call. Bail and re-evaluate
        # all gates against the new head on the next tick.
        return
    if not mergeable:
        # Approved + no human veto + still unmergeable: route to
        # `resolving_conflict` for an automated merge-of-base /
        # conflict-resolve attempt. PyGithub does not distinguish a
        # content conflict from branch-protection / out-of-date-base
        # on `mergeable=False`; we treat any unmergeable PR here as
        # eligible. If the underlying cause is branch protection, the
        # merge attempt is a no-op fast-forward and the `conflict_round`
        # counter still ticks (so a perpetually-unmergeable PR cannot
        # loop in_review <-> resolving_conflict forever).
        #
        # Initialize `conflict_round` only when absent: a PR that bounces
        # back to `in_review` with the counter already set must NOT
        # reset it -- the cap would be ineffective if every re-entry
        # zeroed the counter, and a branch-protection-only PR would
        # ping-pong between handlers indefinitely.
        if state.get("conflict_round") is None:
            state.set("conflict_round", 0)
        try:
            _post_pr_comment(
                gh, int(pr_number), state,
                f":mag: PR is not mergeable; orchestrator is attempting "
                f"auto-resolution by merging "
                f"`{spec.remote_name}/{spec.base_branch}` "
                "into the branch (label: `resolving_conflict`).",
            )
        except Exception:
            log.exception(
                "issue=#%s could not post conflict-resolution notice to "
                "PR #%s", issue.number, pr_number,
            )
        _bump_in_review_watermarks(gh, issue, state)
        gh.emit_event(
            "conflict_round",
            issue_number=issue.number,
            stage="in_review",
            pr_number=int(pr_number),
            sha=head_sha,
            action="entered",
            conflict_round=int(state.get("conflict_round") or 0),
            review_round=int(state.get("review_round") or 0),
            retry_count=state.get("retry_count"),
        )
        gh.set_workflow_label(issue, "resolving_conflict")
        gh.write_pinned_state(issue, state)
        return
    check = gh.pr_combined_check_state(pr)
    if check == "pending":
        return
    if check in ("failure", "none"):
        # 'none' means no checks at all -- ambiguous, refuse to merge.
        _park_awaiting_human(
            gh, issue, state,
            f"{config.HITL_MENTIONS} PR #{pr_number} checks are {check!r}; "
            "refusing to auto-merge.",
            reason="failed_checks",
        )
        state.set("park_reason", "failed_checks")
        _bump_in_review_watermarks(gh, issue, state)
        gh.write_pinned_state(issue, state)
        return

    # Approved + mergeable + green: SHA-pinned merge to the head we GATED
    # on, NOT the (possibly-refreshed) `pr.head.sha`. `pr_is_mergeable`
    # may have refreshed `pr.head.sha` above; using that value here would
    # let a commit landing during the refresh slip through past the
    # approval and changes-requested gates. The SHA-shift check above
    # already bails when this happens, but pinning to `head_sha` here is
    # belt-and-suspenders: GitHub returns 409 for a SHA mismatch so a
    # missed shift cannot merge an unreviewed head.
    merged_ok = gh.merge_pr(pr, sha=head_sha)
    gh.emit_event(
        "merge_attempt",
        issue_number=issue.number,
        stage="in_review",
        pr_number=int(pr_number),
        sha=head_sha,
        method="squash",
        result="success" if merged_ok else "failed",
        check_state=check,
        review_round=int(state.get("review_round") or 0),
        conflict_round=state.get("conflict_round"),
        retry_count=state.get("retry_count"),
    )
    if not merged_ok:
        # 405/409/422 -- next tick will re-evaluate; if it still won't merge,
        # the GH UI shows why.
        return
    state.set("merged_at", _now_iso())
    gh.set_workflow_label(issue, "done")
    gh.write_pinned_state(issue, state)
    gh.emit_event(
        "pr_merged",
        issue_number=issue.number,
        stage="in_review",
        pr_number=int(pr_number),
        sha=head_sha,
        merge_method="squash",
        check_state=check,
        review_round=int(state.get("review_round") or 0),
        conflict_round=state.get("conflict_round"),
        retry_count=state.get("retry_count"),
    )
    try:
        issue.edit(state="closed")
    except Exception:
        log.exception(
            "issue=#%s could not close after auto-merge", issue.number,
        )
    _cleanup_terminal_branch(gh, spec, issue.number)


def _emit_conflict_round_incremented(
    gh: GitHubClient,
    issue: Issue,
    state: PinnedState,
    *,
    pr_number: int,
    new_round: int,
    outcome: str,
    sha: Optional[str] = None,
) -> None:
    """Record a `conflict_round` audit event when the counter ticks.

    Centralizes the bookkeeping so every increment site -- ahead-of-remote
    push recovery, up-to-date no-op flip, clean base-merge push, agent-
    resolved conflict push, drift-pushed bounce -- emits the same shape.
    `outcome` distinguishes the increment cause so a tail of the JSONL sink
    can attribute rounds without re-reading the surrounding code.
    """
    gh.emit_event(
        "conflict_round",
        issue_number=issue.number,
        stage="resolving_conflict",
        pr_number=int(pr_number),
        sha=sha or None,
        action="incremented",
        conflict_round=int(new_round),
        outcome=outcome,
        review_round=int(state.get("review_round") or 0),
        retry_count=state.get("retry_count"),
    )


def _handle_resolving_conflict(
    gh: GitHubClient, spec: RepoSpec, issue: Issue
) -> None:
    """Drive an unmergeable PR back to mergeable.

    Merge `origin/<base>` into the per-issue branch. On a clean merge,
    push and flip back to `validating` so the reviewer agent re-runs on
    the merged head; if the base hasn't moved (branch already
    up-to-date) skip the push and just flip the label. On real content
    conflicts, resume the dev session on the locked backend with a
    conflict-resolution prompt, then push the resolved commit. Cap loops
    via `MAX_CONFLICT_ROUNDS` (parks awaiting human on exhaustion). On
    agent timeout / dirty tree / push failure, park awaiting human and
    let the operator unstick.

    Merge over rebase: simpler (one commit either way) and less
    destructive. Rebase rewrites every commit's SHA, which would
    invalidate any stored `agent_approved_sha` in surprising ways and
    force the reviewer to re-approve the entire branch even when only
    the base content changed.
    """
    state = gh.read_pinned_state(issue)
    pr_number = state.get("pr_number")

    if pr_number is None:
        if state.get("awaiting_human"):
            return
        _park_awaiting_human(
            gh, issue, state,
            f"{config.HITL_MENTIONS} `resolving_conflict` without a pinned "
            "`pr_number`; manual relabeling suspected. Set the workflow "
            "label back to `validating` after fixing.",
            reason="missing_pr_number",
        )
        gh.write_pinned_state(issue, state)
        return

    pr = gh.get_pr(int(pr_number))
    pr_status = gh.pr_state(pr)

    if pr_status == "merged":
        # Mirror the in_review terminal: a human merged the PR (perhaps
        # after manually resolving conflicts) while we were resolving.
        state.set("merged_at", _now_iso())
        gh.set_workflow_label(issue, "done")
        gh.write_pinned_state(issue, state)
        gh.emit_event(
            "pr_merged",
            issue_number=issue.number,
            stage="resolving_conflict",
            pr_number=int(pr_number),
            sha=getattr(pr.head, "sha", None) or None,
            merge_method="external",
            conflict_round=int(state.get("conflict_round") or 0),
            review_round=int(state.get("review_round") or 0),
            retry_count=state.get("retry_count"),
        )
        try:
            issue.edit(state="closed")
        except Exception:
            log.exception(
                "issue=#%s could not close after merge", issue.number,
            )
        _cleanup_terminal_branch(gh, spec, issue.number)
        return

    if pr_status == "closed":
        state.set("closed_without_merge_at", _now_iso())
        gh.set_workflow_label(issue, "rejected")
        gh.write_pinned_state(issue, state)
        gh.emit_event(
            "pr_closed_without_merge",
            issue_number=issue.number,
            stage="resolving_conflict",
            pr_number=int(pr_number),
            sha=getattr(pr.head, "sha", None) or None,
            conflict_round=int(state.get("conflict_round") or 0),
            review_round=int(state.get("review_round") or 0),
            retry_count=state.get("retry_count"),
        )
        try:
            issue.edit(state="closed")
        except Exception:
            log.exception(
                "issue=#%s could not close after reject", issue.number,
            )
        # The PR is gone; clean up the orchestrator-owned branch and
        # worktree. Mirrors the merged-PR cleanup order: finalize GitHub
        # state first, then tidy local + remote refs best-effort.
        _cleanup_terminal_branch(gh, spec, issue.number)
        return

    # PR is open but the issue itself was closed manually (the closed
    # sweep in `list_pollable_issues` yielded it). Mirror in_review's
    # human-stop handling: closing the issue while its PR is still open
    # is a deliberate human signal; flip to `rejected` rather than
    # continuing to spawn the dev agent. Deliberately NOT cleaning the
    # branch here -- the PR is still open and the operator may want to
    # inspect or salvage it.
    #
    # Same caveat as the in_review counterpart: once this flips the
    # label to `rejected`, the dispatcher is a no-op AND the closed-
    # issue sweep in `list_pollable_issues` only covers `in_review` /
    # `resolving_conflict`, so a later PR close is never observed by
    # the orchestrator. The operator must clean up the worktree, local
    # branch, and remote branch manually for the "close issue first,
    # then close PR" ordering.
    if getattr(issue, "state", "open") == "closed":
        state.set("closed_without_merge_at", _now_iso())
        gh.set_workflow_label(issue, "rejected")
        gh.write_pinned_state(issue, state)
        # Deliberately no `pr_closed_without_merge` emit here: the PR is
        # still open. That event is reserved for the actual closed-PR
        # rejection arc above.
        return

    if issue_has_label(issue, BASE_SYNC_HOLD_LABEL):
        log.info(
            "issue=#%d has %r; pausing resolving_conflict base merge",
            issue.number, BASE_SYNC_HOLD_LABEL,
        )
        return

    # User-content drift: a human edited the issue body while the dev
    # was resolving conflicts. Resuming with the new body+comments lets
    # the dev decide whether the edit affects the conflict resolution.
    # On a successful pushed fix we bounce to `validating` so the
    # reviewer re-runs on the updated branch; on an ack (no commit but a
    # reply) we stay in `resolving_conflict` without parking so a
    # harmless clarification doesn't stall the merge.
    new_hash = _detect_user_content_change(gh, issue, state)
    if new_hash is not None:
        state.set("user_content_hash", new_hash)
        _post_pr_comment(
            gh, int(pr_number), state,
            ":pencil2: issue body changed; resuming dev session.",
        )
        # Mark issue-thread comments as consumed: the dev sees the full
        # thread via `_recent_comments_text`, and the eventual
        # validating->in_review handoff (after a successful pushed
        # resolution flips back to validating) must not replay them.
        _mark_drift_comments_consumed(gh, issue, state)
        wt = _worktree_path(spec, issue.number)
        if not wt.exists():
            wt = _ensure_pr_worktree(spec, issue.number)
        before_sha = _head_sha(wt)
        followup = _build_user_content_change_prompt(
            issue, _recent_comments_text(issue),
        )
        wt, result = _resume_dev_with_text(gh, spec, issue, state, followup)
        state.set("last_agent_action_at", _now_iso())
        outcome = _post_user_content_change_result(
            gh, spec, issue, state, wt, result, before_sha,
        )
        if outcome == "pushed":
            conflict_round = int(state.get("conflict_round") or 0)
            state.set("review_round", 0)
            state.set("conflict_round", conflict_round + 1)
            state.set("last_conflict_resolved_at", _now_iso())
            _emit_conflict_round_incremented(
                gh, issue, state,
                pr_number=int(pr_number),
                new_round=conflict_round + 1,
                outcome="drift_resolved",
                sha=_head_sha(wt) or None,
            )
            gh.set_workflow_label(issue, "validating")
        gh.write_pinned_state(issue, state)
        return

    conflict_round = int(state.get("conflict_round") or 0)

    # Resume-on-human-reply: when parked awaiting human and a new
    # comment arrived, resume the dev session on the in-progress merge
    # worktree with the human's text. Mirrors `_handle_implementing`'s
    # awaiting-human path so a `_on_question` / `_on_dirty_worktree`
    # park can be unstuck by a comment (the park messages explicitly
    # invite that flow). Without this branch, those parks would require
    # a manual relabel even though their HITL text says "reply with
    # guidance and the orchestrator will resume the session".
    if state.get("awaiting_human"):
        last_action_id = state.get("last_action_comment_id")
        new_comments = gh.comments_after(issue, last_action_id)
        if not new_comments:
            return  # no human reply yet
        consumed_max = max(c.id for c in new_comments)
        state.set("last_action_comment_id", consumed_max)
        followup = "\n\n".join(
            f"@{c.user.login if c.user else 'user'}: {c.body}"
            for c in new_comments if c.body
        )
        wt = _worktree_path(spec, issue.number)
        if not wt.exists():
            wt = _ensure_pr_worktree(spec, issue.number)
        before_sha = _head_sha(wt)
        wt, result = _resume_dev_with_text(gh, spec, issue, state, followup)
        state.set("last_agent_action_at", _now_iso())
        _post_conflict_resolution_result(
            gh, spec, issue, state, wt, result, before_sha, conflict_round,
        )
        return

    if conflict_round >= config.MAX_CONFLICT_ROUNDS:
        _park_awaiting_human(
            gh, issue, state,
            f"{config.HITL_MENTIONS} auto-conflict-resolution still failing "
            f"after {conflict_round} round(s) "
            f"(`MAX_CONFLICT_ROUNDS={config.MAX_CONFLICT_ROUNDS}`); manual "
            "intervention needed.",
            reason="conflict_cap",
        )
        gh.write_pinned_state(issue, state)
        return

    wt = _worktree_path(spec, issue.number)
    if not wt.exists():
        # PR-aware variant: restores the local branch from
        # `origin/<branch>` if it has been pruned. `_ensure_worktree`
        # would rebuild from `origin/<base>` and silently discard the
        # PR's commits.
        wt = _ensure_pr_worktree(spec, issue.number)

    # Refresh `<remote>/<branch>` (the PR branch's remote tip) via the
    # same hardened authenticated path `_push_branch` uses. We need a
    # current ref before the ahead/behind check below: a stale local
    # `<remote>/<branch>` would mis-classify a real "remote moved out from
    # under us" situation as in-sync.
    branch = _branch_name(issue.number)
    fetch_branch = _authed_fetch(
        spec,
        f"+refs/heads/{branch}:refs/remotes/{spec.remote_name}/{branch}",
        cwd=wt,
    )
    if fetch_branch.returncode != 0:
        log.error(
            "issue=#%d branch fetch failed in resolving_conflict: %s",
            issue.number, (fetch_branch.stderr or "").strip(),
        )
        _park_awaiting_human(
            gh, issue, state,
            f"{config.HITL_MENTIONS} `git fetch {spec.remote_name} {branch}` "
            "failed during conflict resolution; see orchestrator logs.",
            reason="fetch_failed",
        )
        gh.write_pinned_state(issue, state)
        return

    # Check the worktree against the freshly-fetched remote PR head.
    # Three outcomes:
    #   * `(0, 0)`: in sync -- proceed to the base-merge below.
    #   * `(>0, 0)`: HEAD has unpushed commits ahead of the remote PR
    #     head. This is the crash-recovery case: a previous tick committed
    #     a conflict resolution but crashed before `_push_branch` returned
    #     (or before the post-push state write landed). Without this
    #     branch the next tick's `git merge` would be a no-op (HEAD
    #     already contains origin/<base>) and we would flip to validating
    #     with the dev's resolution still unpushed -- letting the reviewer
    #     vote on a SHA that is not on the PR. Mirrors the implementing
    #     handler's `_has_new_commits` recovery shortcut.
    #   * Anything with `behind > 0`: stale or diverged worktree. Force-
    #     pushing the local state would clobber the real PR head, and
    #     merging origin/<base> into a stale branch then force-pushing
    #     would silently revert anything that landed on `origin/<branch>`
    #     out-of-band. Refuse and park.
    ahead, behind = _branch_ahead_behind(spec, wt, branch)
    if behind > 0:
        _park_awaiting_human(
            gh, issue, state,
            f"{config.HITL_MENTIONS} worktree on `{branch}` is {ahead} "
            f"ahead and {behind} behind `{spec.remote_name}/{branch}` "
            f"(PR head `{pr.head.sha[:8]}`); refusing to merge a stale "
            "or diverged branch -- force-pushing the local state would "
            "clobber the real PR head. Manual intervention needed.",
            reason="diverged_branch",
        )
        gh.write_pinned_state(issue, state)
        return
    if ahead > 0:
        # Dirty check before pushing recovered work: if the previous
        # tick crashed before its own dirty check ran, the worktree
        # may carry uncommitted edits that the unpushed commit does
        # NOT contain. Pushing in that state would publish a SHA that
        # silently omits those edits, and the reviewer at validating
        # would later run on a local tree that does not match the PR.
        # Mirror `_on_dirty_worktree`: park awaiting human, no flip.
        dirty = _worktree_dirty_files(wt)
        if dirty:
            _park_awaiting_human(
                gh, issue, state,
                f"{config.HITL_MENTIONS} worktree has {len(dirty)} "
                "uncommitted change(s) alongside recovered conflict "
                "resolution; refusing to push an incomplete branch. "
                "Resolve the dirty tree manually before resuming.",
                reason="dirty_worktree",
            )
            gh.write_pinned_state(issue, state)
            return
        log.info(
            "issue=#%d resolving_conflict: pushing %d recovered commit(s) "
            "ahead of %s/%s before attempting base merge",
            issue.number, ahead, spec.remote_name, branch,
        )
        if not _push_branch(spec, wt, branch):
            _park_awaiting_human(
                gh, issue, state,
                f"{config.HITL_MENTIONS} git push of recovered conflict "
                "resolution failed; see orchestrator logs.",
                reason="push_failed",
            )
            gh.write_pinned_state(issue, state)
            return
        state.set("review_round", 0)
        state.set("conflict_round", conflict_round + 1)
        state.set("last_conflict_resolved_at", _now_iso())
        _emit_conflict_round_incremented(
            gh, issue, state,
            pr_number=int(pr_number),
            new_round=conflict_round + 1,
            outcome="recovered_push",
            sha=_head_sha(wt) or None,
        )
        gh.set_workflow_label(issue, "validating")
        gh.write_pinned_state(issue, state)
        return

    # In sync. Refresh `<remote>/<base>` so the upcoming
    # `git merge <remote>/<base>` sees the current base tip.
    fetch_base = _authed_fetch(
        spec,
        f"+refs/heads/{spec.base_branch}:"
        f"refs/remotes/{spec.remote_name}/{spec.base_branch}",
        cwd=wt,
    )
    if fetch_base.returncode != 0:
        log.error(
            "issue=#%d base fetch failed in resolving_conflict: %s",
            issue.number, (fetch_base.stderr or "").strip(),
        )
        _park_awaiting_human(
            gh, issue, state,
            f"{config.HITL_MENTIONS} "
            f"`git fetch {spec.remote_name} {spec.base_branch}` "
            "failed during conflict resolution; see orchestrator logs.",
            reason="fetch_failed",
        )
        gh.write_pinned_state(issue, state)
        return

    before_sha = _head_sha(wt)
    succeeded, conflicted_files = _merge_base_into_worktree(spec, wt)
    gh.emit_event(
        "merge_attempt",
        issue_number=issue.number,
        stage="resolving_conflict",
        pr_number=int(pr_number),
        sha=before_sha or None,
        method="base_merge",
        result="success" if succeeded else (
            "conflict" if conflicted_files else "failed"
        ),
        conflict_round=conflict_round,
        review_round=int(state.get("review_round") or 0),
        retry_count=state.get("retry_count"),
    )

    if succeeded:
        # Dirty check before EITHER clean-merge exit (no-op flip OR
        # merge-commit push): a pre-existing uncommitted edit (left by a
        # previous tick that crashed before its own dirty check ran)
        # would otherwise survive a no-op flip into validating, where
        # the reviewer agent reads the worktree directly. The reviewer
        # would then vote on a tree that does NOT match the PR head;
        # AUTO_MERGE would later refuse the SHA mismatch but the agent
        # approval is already sitting against an incorrect SHA. Park
        # rather than push or flip in that state, mirroring
        # `_on_dirty_worktree`'s "refuse to publish an incomplete
        # branch" rule.
        dirty = _worktree_dirty_files(wt)
        if dirty:
            _park_awaiting_human(
                gh, issue, state,
                f"{config.HITL_MENTIONS} worktree has {len(dirty)} "
                f"uncommitted change(s) after `git merge "
                f"{spec.remote_name}/{spec.base_branch}`; refusing to "
                "push or hand back to validating with a dirty tree.",
                reason="dirty_worktree",
            )
            gh.write_pinned_state(issue, state)
            return
        after_sha = _head_sha(wt)
        if not after_sha or after_sha == before_sha:
            # Already up-to-date with base. Nothing to push -- just hand
            # back to validating and let AUTO_MERGE re-evaluate.
            #
            # Increment `conflict_round` even though no diff was applied:
            # if the PR is unmergeable purely due to branch protection /
            # required reviewers (PyGithub cannot distinguish those from a
            # content conflict), the no-op merge would otherwise loop
            # in_review <-> resolving_conflict forever with the cap never
            # firing. Counting the no-op against the cap surfaces the
            # situation to the operator within `MAX_CONFLICT_ROUNDS` ticks.
            log.info(
                "issue=#%d resolving_conflict: branch already up-to-date "
                "with %s/%s", issue.number,
                spec.remote_name, spec.base_branch,
            )
            state.set("review_round", 0)
            state.set("conflict_round", conflict_round + 1)
            _emit_conflict_round_incremented(
                gh, issue, state,
                pr_number=int(pr_number),
                new_round=conflict_round + 1,
                outcome="base_up_to_date",
                sha=after_sha,
            )
            gh.set_workflow_label(issue, "validating")
            gh.write_pinned_state(issue, state)
            return
        if not _push_branch(spec, wt, _branch_name(issue.number)):
            _park_awaiting_human(
                gh, issue, state,
                f"{config.HITL_MENTIONS} git push failed after auto-merging "
                f"`{spec.remote_name}/{spec.base_branch}`; "
                "see orchestrator logs.",
                reason="push_failed",
            )
            gh.write_pinned_state(issue, state)
            return
        state.set("review_round", 0)
        state.set("conflict_round", conflict_round + 1)
        state.set("last_conflict_resolved_at", _now_iso())
        _emit_conflict_round_incremented(
            gh, issue, state,
            pr_number=int(pr_number),
            new_round=conflict_round + 1,
            outcome="base_merged_clean",
            sha=after_sha,
        )
        gh.set_workflow_label(issue, "validating")
        gh.write_pinned_state(issue, state)
        return

    if not conflicted_files:
        _park_awaiting_human(
            gh, issue, state,
            f"{config.HITL_MENTIONS} "
            f"`git merge {spec.remote_name}/{spec.base_branch}` "
            "failed without listing conflicted files; manual intervention "
            "needed.",
            reason="merge_failed_no_files",
        )
        gh.write_pinned_state(issue, state)
        return

    fix_prompt = _build_conflict_resolution_prompt(
        f"{spec.remote_name}/{spec.base_branch}", conflicted_files,
    )
    wt, result = _resume_dev_with_text(gh, spec, issue, state, fix_prompt)
    state.set("last_agent_action_at", _now_iso())
    _post_conflict_resolution_result(
        gh, spec, issue, state, wt, result, before_sha, conflict_round,
    )


def _post_conflict_resolution_result(
    gh: GitHubClient,
    spec: RepoSpec,
    issue: Issue,
    state: PinnedState,
    wt: Path,
    result: AgentResult,
    before_sha: str,
    conflict_round: int,
) -> None:
    """Common post-agent handling for both fresh conflict resolution
    and the awaiting-human resume path in `_handle_resolving_conflict`.

    Always calls `gh.write_pinned_state` before returning so the caller
    can return immediately after invoking this helper. Increments
    `conflict_round` only on the success path -- failure paths leave
    the counter alone so a human-reply resume that lands cleanly still
    consumes a slot, but a timeout/dirty/push-failure on the same
    counter does not.
    """
    if result.timed_out:
        _park_awaiting_human(
            gh, issue, state,
            f"{config.HITL_MENTIONS} dev agent timed out resolving merge "
            f"conflicts after {config.AGENT_TIMEOUT}s; manual intervention "
            "needed.",
            reason="agent_timeout",
        )
        gh.write_pinned_state(issue, state)
        return

    after_sha = _head_sha(wt)
    if not after_sha or after_sha == before_sha:
        # Agent did not produce a merge commit. Treat as a question /
        # silence park, mirroring the implementing handler.
        _on_question(gh, issue, state, result)
        gh.write_pinned_state(issue, state)
        return

    dirty = _worktree_dirty_files(wt)
    if dirty:
        _on_dirty_worktree(gh, issue, state, result, dirty)
        gh.write_pinned_state(issue, state)
        return

    if not _push_branch(spec, wt, _branch_name(issue.number)):
        _park_awaiting_human(
            gh, issue, state,
            f"{config.HITL_MENTIONS} git push failed after conflict "
            "resolution; see orchestrator logs.",
            reason="push_failed",
        )
        gh.write_pinned_state(issue, state)
        return

    state.set("review_round", 0)
    state.set("conflict_round", conflict_round + 1)
    state.set("last_conflict_resolved_at", _now_iso())
    pr_number = state.get("pr_number")
    if pr_number is not None:
        _emit_conflict_round_incremented(
            gh, issue, state,
            pr_number=int(pr_number),
            new_round=conflict_round + 1,
            outcome="agent_resolved",
            sha=after_sha,
        )
    gh.set_workflow_label(issue, "validating")
    gh.write_pinned_state(issue, state)


def _on_commits(
    gh: GitHubClient,
    spec: RepoSpec,
    issue: Issue,
    state: PinnedState,
    result: AgentResult,
) -> None:
    wt = _worktree_path(spec, issue.number)
    branch = _branch_name(issue.number)
    if not _push_branch(spec, wt, branch):
        # Park on awaiting_human like the timeout/question paths. Otherwise the
        # worktree's commits keep _has_new_commits() true, so every poll would
        # re-enter _on_commits() and re-comment indefinitely until a human acts.
        _park_awaiting_human(
            gh, issue, state,
            f"{config.HITL_MENTIONS} git push failed; see orchestrator logs.",
            reason="push_failed",
        )
        # _handle_implementing writes pinned state after we return.
        return
    # Recover gracefully if a previous tick crashed between open_pr and the
    # relabel: reuse the existing open PR instead of 422-ing on duplicate.
    pr = gh.find_open_pr(branch=branch, base=spec.base_branch)
    if pr is None:
        title = _pr_title_from_commit_or_issue(issue, _first_commit_subject(spec, wt))
        _, dev_backend, _, dev_sid = _read_dev_session(state)
        body_parts = [
            f"Resolves #{issue.number}",
            "",
            f"Generated by orchestrator ({dev_backend} session `{dev_sid or '?'}`).",
        ]
        if result.last_message.strip():
            body_parts += ["", "---", "_Last agent message:_", "", result.last_message[:2000]]
        pr = gh.open_pr(
            branch=branch, base=spec.base_branch, title=title, body="\n".join(body_parts)
        )
        _post_issue_comment(gh, issue, state, f":sparkles: PR opened: #{pr.number}")
        gh.emit_event(
            "pr_opened",
            issue_number=issue.number,
            stage="implementing",
            pr_number=pr.number,
            branch=branch,
            sha=getattr(pr.head, "sha", None) or None,
            retry_count=state.get("retry_count"),
        )
    else:
        log.info("issue=#%s reusing existing PR #%d for %s", issue.number, pr.number, branch)
    state.set("pr_number", pr.number)
    # Reset the review counter every time we (re-)open a PR so the validating
    # handler starts fresh on the new branch state.
    state.set("review_round", 0)
    # Issue moved forward; reset the implementing retry budget so any future
    # bounce back into implementing (e.g. validating -> implementing in a
    # later stage) starts with a fresh window.
    state.set("retry_count", 0)
    state.set("retry_window_start", None)
    # The session just produced commits, so it isn't poisoned -- reset the
    # silent-park streak so a future blip doesn't tip an otherwise-healthy
    # session past the fresh-session threshold.
    state.set("silent_park_count", 0)
    gh.set_workflow_label(issue, "validating")


def _on_question(
    gh: GitHubClient, issue: Issue, state: PinnedState, result: AgentResult
) -> None:
    raw = result.last_message.strip()
    if raw:
        quoted = "> " + raw.replace("\n", "\n> ")
        _post_issue_comment(
            gh, issue, state,
            f"{config.HITL_MENTIONS} agent needs your input to proceed:\n\n{quoted}",
        )
        state.set("awaiting_human", True)
        # Real question parks are not transient: they need a human reply
        # before the auto-merge gates should run again. Clear any stale
        # `park_reason` left behind by a prior AUTO_MERGE failed_checks /
        # unmergeable park, and reset the silent-park streak.
        state.set("park_reason", None)
        state.set("silent_park_count", 0)
        park_reason = "agent_question"
    else:
        # No commits AND no final message -- the agent produced literally
        # nothing. Callers only invoke `_on_question` when the worktree has
        # no new commits, so an empty `last_message` here is a silent
        # failure, not a content question. The most common cause is a
        # poisoned resume of a session previously killed mid-stream (e.g.
        # by a Claude rate limit). Tag the park with a distinct reason so
        # `_resume_dev_with_text` can drop the dev session id after enough
        # consecutive silent parks, and surface the situation accurately
        # to the operator instead of impersonating a real "agent has a
        # question" park.
        diag = _format_stderr_diagnostics(result, "Agent")
        _post_issue_comment(
            gh, issue, state,
            f"{config.HITL_MENTIONS} agent produced no output (likely a "
            f"session-resume failure); manual intervention needed.{diag}",
        )
        log.warning(
            "issue=#%s agent produced no output; exit_code=%d "
            "timed_out=%s stderr_tail=%r",
            issue.number, result.exit_code, result.timed_out,
            _stderr_log_tail(result),
        )
        state.set("awaiting_human", True)
        state.set("park_reason", "agent_silent")
        state.set(
            "silent_park_count",
            int(state.get("silent_park_count") or 0) + 1,
        )
        park_reason = "agent_silent"
    latest = gh.latest_comment_id(issue)
    if latest is not None:
        state.set("last_action_comment_id", latest)
    gh.emit_event(
        "park_awaiting_human",
        issue_number=issue.number,
        stage=gh.workflow_label(issue),
        reason=park_reason,
    )


def _on_dirty_worktree(
    gh: GitHubClient,
    issue: Issue,
    state: PinnedState,
    result: AgentResult,
    dirty: list[str],
) -> None:
    """Park instead of pushing when the agent left uncommitted changes.

    Pushing here would publish a branch that omits the dirty files, so the PR
    would not match what the agent actually produced. We surface the situation
    to the human and resume the codex session on their reply, identical to the
    question path.
    """
    shown = dirty[:10]
    files_md = "\n".join(f"- `{p}`" for p in shown)
    if len(dirty) > len(shown):
        files_md += f"\n- … ({len(dirty) - len(shown)} more)"
    last_msg = result.last_message.strip()
    tail = ""
    if last_msg:
        quoted = "> " + last_msg.replace("\n", "\n> ")
        tail = f"\n\n_Last agent message:_\n\n{quoted}"
    _post_issue_comment(
        gh, issue, state,
        f"{config.HITL_MENTIONS} agent committed but left {len(dirty)} "
        f"uncommitted change(s); refusing to push an incomplete branch. "
        f"Reply with guidance and the orchestrator will resume the session.\n\n"
        f"{files_md}{tail}",
    )
    state.set("awaiting_human", True)
    # Mirror `_on_question`: not transient, clear any stale `park_reason`
    # so a prior AUTO_MERGE transient park does not auto-recover over the
    # standing dirty-worktree question. Clear the silent-park streak too:
    # the agent produced output, so the session is not poisoned.
    state.set("park_reason", None)
    state.set("silent_park_count", 0)
    latest = gh.latest_comment_id(issue)
    if latest is not None:
        state.set("last_action_comment_id", latest)
    gh.emit_event(
        "park_awaiting_human",
        issue_number=issue.number,
        stage=gh.workflow_label(issue),
        reason="dirty_worktree",
        dirty_files=len(dirty),
    )
