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
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

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
    _build_conflict_resolution_prompt,
    _orchestrator_ids,
    _post_issue_comment,
    _post_pr_comment,
    _recent_comments_text,
)
# Consumed only by `stages/in_review.py` (via `_wf.<name>`) and by tests
# that read them as `workflow.<name>`. The redundant `as <name>` aliasing
# is the pyflakes/ruff convention for marking an import as an intentional
# re-export so F401 does not flag the name as unused.
from .workflow_messages import _ORCH_COMMENT_MARKER as _ORCH_COMMENT_MARKER
from .workflow_messages import (
    _build_pr_comment_followup as _build_pr_comment_followup,
)
# The names below are now consumed only by the stage modules under
# `orchestrator.stages` (via `_wf.<name>`) and by tests that read them as
# `workflow.<name>`. Re-export each one explicitly with `as <name>` so the
# attribute stays on this module without tripping the F401 "unused import"
# lint -- the redundant alias is the pyflakes/ruff convention for marking
# an import as an intentional re-export.
from .workflow_messages import _build_fix_prompt as _build_fix_prompt
from .workflow_messages import _build_implement_prompt as _build_implement_prompt
from .workflow_messages import _build_review_prompt as _build_review_prompt
from .workflow_messages import _drift_ack_reason as _drift_ack_reason
from .workflow_messages import (
    _format_stderr_diagnostics as _format_stderr_diagnostics,
)
from .workflow_messages import _parse_review_verdict as _parse_review_verdict
from .workflow_messages import _stderr_log_tail as _stderr_log_tail
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
# Validating-stage handler and the reviewer-session lifecycle live in
# `stages/validating.py`. Re-export under the original names so direct
# `workflow._foo` references in the test suite, the dispatcher, and the
# in_review / resolving_conflict handlers (which call `_handle_dev_fix_result`
# / `_post_user_content_change_result` unqualified) keep working. The
# stage module accesses callees it needs back here (`_park_awaiting_human`,
# `_run_agent_tracked`, `_now_iso`, the worktree plumbing, the messaging
# helpers, ...) through `from .. import workflow as _wf` at call time, so
# test patches against `workflow._foo` still take effect when the call
# originates inside the stage module.
from .stages.validating import (
    _VALIDATING_TRANSIENT_PARK_REASONS as _VALIDATING_TRANSIENT_PARK_REASONS,
)
from .stages.validating import _handle_dev_fix_result as _handle_dev_fix_result
from .stages.validating import _handle_validating as _handle_validating
from .stages.validating import _latest_pr_comment_ids as _latest_pr_comment_ids
from .stages.validating import (
    _post_user_content_change_result as _post_user_content_change_result,
)
from .stages.validating import _seed_watermark_past_self as _seed_watermark_past_self
from .stages.validating import (
    _try_recover_validating_transient_park as _try_recover_validating_transient_park,
)
# In-review-stage handler and the PR-side primitives it owns live in
# `stages/in_review.py`. Re-export under the original names so direct
# `workflow._handle_in_review` references in the test suite and the
# dispatcher keep working. The stage module accesses callees it needs
# back here (`_park_awaiting_human`, `_handle_dev_fix_result`,
# `_post_user_content_change_result`, `_resume_dev_with_text`, `_now_iso`,
# the worktree plumbing, the drift / messaging helpers, ...) through
# `from .. import workflow as _wf` at call time, so test patches against
# `workflow._foo` still take effect when the call originates inside the
# stage module.
from .stages.in_review import _TRANSIENT_PARK_REASONS as _TRANSIENT_PARK_REASONS
from .stages.in_review import (
    _auto_merge_gates_pass as _auto_merge_gates_pass,
)
from .stages.in_review import (
    _bump_in_review_watermarks as _bump_in_review_watermarks,
)
from .stages.in_review import _comment_created_at as _comment_created_at
from .stages.in_review import _handle_in_review as _handle_in_review
from .stages.in_review import (
    _seed_legacy_in_review_watermarks as _seed_legacy_in_review_watermarks,
)

log = logging.getLogger(__name__)


# Workflow labels whose handlers can read or write OTHER issues' pinned
# state -- the cross-issue writers are:
#   * `_handle_decomposing` -- creates child issues, seeds their pinned
#     state, may flip their labels (`set_workflow_label(child, "ready")`),
#     and the half-finished recovery branch seeds `parent_number` on each
#     already-recorded child.
#   * `_handle_blocked` -- the dep-graph walk flips no-longer-blocked
#     children from `blocked` to `ready` (`set_workflow_label(child, ...)`).
#   * `_handle_umbrella` -- the dep-graph walk plus the close-on-all-done
#     branch can flip child labels too.
#   * `_handle_pickup` (no label) -- routes straight into
#     `_handle_decomposing`, so a freshly arrived unlabeled issue can
#     create children on the same tick.
# Running two of these in parallel can race a parent's child-state write
# against the child's own handler on a sibling thread (the original
# reproducer: a decomposing parent seeded `parent_number` on a child while
# the same child's `_handle_blocked` parked `blocked_no_children` and
# clobbered the seed).
#
# `_handle_ready` is NOT in this set. It writes only its own pinned state
# and label, then recurses into `_handle_implementing` (also own-state
# only). Multiple `ready` issues on the same tick must therefore be free
# to fan out across worker threads so the long-running agent work is
# actually concurrent under `parallel_limit > 1`. The earlier draft put
# `ready` here and serialized those agent jobs, defeating the issue's
# concurrency goal.
#
# `tick()` submits the family-aware bucket to the executor as ONE drain
# task that processes its issues sequentially on a single worker thread;
# each non-family-aware issue gets its own task. Folding the family
# bucket into one task caps its executor footprint at exactly one slot
# regardless of how many family-aware issues are pending, so the other
# `limit - 1` slots stay free for fanout. (Submitting per-family-issue
# futures with a shared lock would let waiting family futures occupy
# additional worker slots and starve fanout under a small `limit`.)
# This preserves the "no two cross-issue writers at once" invariant
# while keeping a slow decomposing / unlabeled-pickup handler from
# blocking unrelated implementing / validating issues on the same
# tick. Stages outside this set (`ready`, `implementing`,
# `validating`, `in_review`, `resolving_conflict`) only read and write
# their own per-issue state + worktree, so they stay eligible for
# unconditional parallel fan-out.
_FAMILY_AWARE_LABELS = frozenset({
    "decomposing", "blocked", "umbrella",
})


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
    # parallel_limit==1 (the legacy default) keeps the sequential iteration
    # in-thread AND keeps streaming directly over `gh.list_pollable_issues()`
    # rather than materializing the list first. Materializing here would
    # change observable behavior on a partial enumeration failure (e.g. a
    # PyGithub pagination error mid-sweep): the legacy loop processes
    # everything yielded BEFORE the failure, but a `list(...)` upfront
    # would lose every already-yielded issue when the generator raises.
    # limit>1 fans the per-issue work out across a bounded thread pool;
    # each `_process_issue` is independent (per-issue worktree, per-issue
    # PinnedState, per-issue GitHub label/comment surface) so threads
    # serialize only at the PyGithub HTTP layer, which is already
    # thread-safe.
    #
    # The effective cap is the smaller of the per-repo `parallel_limit` and
    # the host-wide `config.MAX_PARALLEL_ISSUES_GLOBAL`. The global ceiling
    # is documented as applying regardless of any one repo's per-repo cap
    # so an operator can configure a high `parallel_limit` per repo
    # without exceeding the host's CPU/memory budget for agent CLIs.
    # `main._run_tick` already runs the per-repo ticks sequentially, so
    # capping inside `tick()` is sufficient to bound concurrent worker
    # threads at any given moment to `MAX_PARALLEL_ISSUES_GLOBAL`.
    per_repo_limit = max(1, int(getattr(spec, "parallel_limit", 1) or 1))
    global_limit = max(
        1, int(getattr(config, "MAX_PARALLEL_ISSUES_GLOBAL", 1) or 1)
    )
    limit = min(per_repo_limit, global_limit)
    if limit == 1:
        for issue in gh.list_pollable_issues():
            try:
                _process_issue(gh, spec, issue)
            except Exception:
                log.exception(
                    "repo=%s issue=#%s processing failed",
                    spec.slug, issue.number,
                )
        return
    # Parallel path: the executor needs the full submission set up front to
    # bound `max_workers` correctly, so the generator is materialized here.
    # The trade-off is consistent with the parallel mode's intent (fan out
    # the whole eligible set this tick); on an enumeration failure the
    # whole tick aborts -- the next tick's enumeration will retry.
    #
    # Partition by `_FAMILY_AWARE_LABELS`: stages that read/write across
    # parent/child boundaries must never run two at a time -- a parent's
    # `_handle_decomposing` recovery seeds `parent_number` on a child
    # while the child's `_handle_blocked` would otherwise clobber the
    # same pinned-state comment. The remaining issues fan out across the
    # worker pool because their handlers touch only per-issue state.
    #
    # Both buckets share a single executor capped at `limit`, and the
    # family-aware workers acquire a tick-local lock around their
    # `_process_issue` call so they cannot overlap with each other. They
    # CAN overlap with non-family workers: a slow decomposing /
    # unlabeled-pickup agent run on one worker no longer blocks the
    # other `limit-1` workers from advancing unrelated implementing /
    # validating issues in the same tick. Without this overlap a mixed
    # tick with one long decomposing issue and several ready /
    # implementing issues would still process those implementing issues
    # serially after the decomposer finished -- the opposite of what
    # `parallel_limit > 1` is supposed to deliver.
    #
    # Label is read on the caller thread to avoid an extra worker-side
    # GitHub round-trip just to bucket the issue. Per-issue exception
    # isolation extends to that label read: a PyGithub lazy-load failure
    # on one issue's labels must not abort the whole tick before the
    # other eligible issues are even classified. A failing read is
    # logged and the issue is conservatively routed into the family
    # bucket, where the per-issue try/except below catches any sustained
    # failure with the same log line shape the rest of `tick` produces.
    family_numbers: list[int] = []
    fanout_numbers: list[int] = []
    for issue in gh.list_pollable_issues():
        try:
            label = gh.workflow_label(issue)
        except Exception:
            log.exception(
                "repo=%s issue=#%s workflow_label read failed; routing to "
                "family bucket so per-issue exception isolation can pick "
                "up any sustained failure", spec.slug, issue.number,
            )
            family_numbers.append(issue.number)
            continue
        if label is None or label in _FAMILY_AWARE_LABELS:
            family_numbers.append(issue.number)
        else:
            fanout_numbers.append(issue.number)

    if not family_numbers and not fanout_numbers:
        return
    # The family bucket is submitted as a SINGLE drain task that
    # processes its issues sequentially on one worker thread. With
    # `parallel_limit=2`, two family issues, and one fanout issue,
    # submitting two family futures plus one fanout future would let a
    # slow first family handler hold one worker slot while the second
    # family future occupied the other worker slot blocking on a
    # family lock -- the fanout issue would be queued and could not run
    # until the slow family handler exited, defeating mixed-stage
    # concurrency. Folding the whole family bucket into one drain task
    # caps its footprint at exactly one executor slot regardless of how
    # many family-aware issues there are, leaving the other `limit-1`
    # slots free for fanout.
    total_tasks = (1 if family_numbers else 0) + len(fanout_numbers)
    # max_workers is capped at `limit` AND at the submitted-task count
    # so a quiet tick (e.g. one fan-out issue) does not spin up idle
    # worker threads.
    workers = min(limit, total_tasks)

    # Only issue NUMBERS cross the thread boundary. PyGithub's `Issue`
    # and the parent `GitHubClient`/`Repository`/`Requester` chain hold
    # mutable per-request state that is not documented as thread-safe;
    # passing an Issue resolved on the caller thread into a worker
    # thread would have that worker drive a shared `Requester` and
    # could corrupt concurrent GitHub operations. Each worker instead
    # calls `gh._for_worker_thread()` to mint a fresh client (= fresh
    # Github + Requester + Repository) and refetches its Issue against
    # THAT client, so every in-flight HTTP call is the sole consumer of
    # its requester's state. The fake mirrors `_for_worker_thread` to
    # return `self`, so tests keep their single-fake assertion model.

    def _drain_family_bucket() -> None:
        """Process every family-aware issue this tick sequentially.

        Per-issue exception isolation lives INSIDE this function (one
        try/except per issue) so the family bucket keeps draining if any
        single family handler raises; without that, the as_completed
        loop below would see one swallowed-or-raising future for the
        whole bucket. The function itself never raises -- the outer
        loop's `fut.result()` call therefore only ever logs a
        programming-level failure for this future.
        """
        for issue_number in family_numbers:
            try:
                worker_gh = gh._for_worker_thread()
                worker_issue = worker_gh.get_issue(issue_number)
                _process_issue(worker_gh, spec, worker_issue)
            except Exception:
                log.exception(
                    "repo=%s issue=#%s processing failed",
                    spec.slug, issue_number,
                )

    def _run_fanout_in_worker(issue_number: int) -> None:
        worker_gh = gh._for_worker_thread()
        worker_issue = worker_gh.get_issue(issue_number)
        _process_issue(worker_gh, spec, worker_issue)

    with ThreadPoolExecutor(
        max_workers=workers,
        thread_name_prefix=f"orch-{spec.slug.replace('/', '__')}",
    ) as ex:
        # Sentinel for the family bucket future so the as_completed loop
        # can distinguish it from per-fanout-issue futures.
        family_sentinel: object = object()
        futures: dict[Any, Any] = {}
        if family_numbers:
            futures[ex.submit(_drain_family_bucket)] = family_sentinel
        for n in fanout_numbers:
            futures[ex.submit(_run_fanout_in_worker, n)] = n
        # `as_completed` so a slow issue does not delay logging the failures
        # of faster ones. Each `fut.result()` is wrapped individually so one
        # raising issue cannot abort the remaining futures' result drain.
        for fut in as_completed(futures):
            tag = futures[fut]
            try:
                fut.result()
            except Exception:
                if tag is family_sentinel:
                    # `_drain_family_bucket` catches per-issue exceptions
                    # itself; reaching here means a programming error in
                    # the drain loop. Log it loudly but don't kill the
                    # remaining (fanout) futures' drain.
                    log.exception(
                        "repo=%s family bucket drain raised (programming "
                        "error -- per-issue exceptions are handled inside "
                        "the drain)", spec.slug,
                    )
                else:
                    log.exception(
                        "repo=%s issue=#%s processing failed",
                        spec.slug, tag,
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
