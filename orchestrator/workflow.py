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
from datetime import datetime, timezone
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
    _detect_user_content_change,
    _mark_drift_comments_consumed,
)
from .workflow_drift import _compute_user_content_hash as _compute_user_content_hash
from .workflow_drift import _route_drift_to_decomposing as _route_drift_to_decomposing
from .workflow_messages import (
    _ORCH_COMMENT_MARKER,
    _build_conflict_resolution_prompt,
    _build_fix_prompt,
    _build_pr_comment_followup,
    _build_review_prompt,
    _drift_ack_reason,
    _format_stderr_diagnostics,
    _orchestrator_ids,
    _parse_review_verdict,
    _post_issue_comment,
    _post_pr_comment,
    _recent_comments_text,
    _stderr_log_tail,
)
# `_build_implement_prompt` is now consumed only by `stages/implementing.py`
# (via `_wf._build_implement_prompt`) and by tests that read it as
# `workflow._build_implement_prompt`. Re-export it explicitly so the attribute
# stays on this module without tripping the F401 "unused import" lint.
from .workflow_messages import _build_implement_prompt as _build_implement_prompt
# Re-exports for backward-compatible `workflow._foo` references in the test
# suite. Each name is the helper module's authoritative definition; the
# redundant `as <name>` aliasing is the pyflakes/ruff convention for marking
# an import as an intentional re-export so F401 does not flag it.
from .workflow_messages import _build_decompose_prompt as _build_decompose_prompt
from .workflow_messages import _DRIFT_ACK_RE as _DRIFT_ACK_RE
from .workflow_messages import _parse_manifest as _parse_manifest
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
# Decomposition-stage handlers and their stage-private helpers live in
# `stages/decomposition.py`. Re-export under the original names so direct
# `workflow._handle_*` references in the test suite and the dispatcher
# below keep working. The stage module accesses callees it needs back
# here (`_park_awaiting_human`, `_run_agent_tracked`, the worktree
# plumbing, `_handle_implementing`, ...) through `from .. import workflow`
# at call time, so test patches against `workflow._foo` still take
# effect when the call originates inside the stage module.
from .stages.decomposition import _handle_blocked as _handle_blocked
from .stages.decomposition import _handle_decomposing as _handle_decomposing
from .stages.decomposition import _handle_ready as _handle_ready
from .stages.decomposition import _handle_umbrella as _handle_umbrella
from .stages.decomposition import (
    _read_decomposer_session as _read_decomposer_session,
)
from .stages.decomposition import (
    _resume_decomposer_on_human_reply as _resume_decomposer_on_human_reply,
)
# Implementing-stage handlers and the developer-session lifecycle live in
# `stages/implementing.py`. Re-export under the original names so direct
# `workflow._handle_*` / `workflow._on_*` references in the test suite and
# the dispatcher / other stage handlers keep working. The stage module
# accesses callees it needs back here (`_park_awaiting_human`,
# `_run_agent_tracked`, `_now_iso`, the worktree plumbing, ...) through
# `from .. import workflow as _wf` at call time, so test patches against
# `workflow._foo` still take effect when the call originates inside the
# stage module.
from .stages.implementing import (
    _CLAUDE_STALE_SESSION_STDERR_MARKERS as _CLAUDE_STALE_SESSION_STDERR_MARKERS,
)
from .stages.implementing import (
    _SILENT_PARKS_BEFORE_FRESH_SESSION as _SILENT_PARKS_BEFORE_FRESH_SESSION,
)
from .stages.implementing import (
    _check_and_increment_retry_budget as _check_and_increment_retry_budget,
)
from .stages.implementing import (
    _drop_poisoned_dev_session as _drop_poisoned_dev_session,
)
from .stages.implementing import _handle_implementing as _handle_implementing
from .stages.implementing import (
    _is_stale_session_failure as _is_stale_session_failure,
)
from .stages.implementing import _on_commits as _on_commits
from .stages.implementing import _on_dirty_worktree as _on_dirty_worktree
from .stages.implementing import _on_question as _on_question
from .stages.implementing import _read_dev_session as _read_dev_session
from .stages.implementing import _resume_dev_with_text as _resume_dev_with_text
from .stages.implementing import (
    _resume_developer_on_human_reply as _resume_developer_on_human_reply,
)

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
