# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""State machine: drive issues through the orchestrator workflow.

(no label) -> implementing -> validating -> documenting (final-docs)
-> in_review -> done|rejected.
After the implementer commits and the PR opens, `_on_commits` relabels
straight to `validating` -- the docs pass only runs as the final-docs
handoff after the reviewer approves, not as a pre-review hop. Validating
then runs a fresh reviewer session; on changes-requested the dev session
is resumed, the fix pushed, and the reviewer reruns until APPROVED or
MAX_REVIEW_ROUNDS is hit (the issue stays on `validating` throughout
these fix rounds -- the single docs pass is deferred to the final-docs
handoff after reviewer approval). After approval (+ verify + squash)
the handler relabels to `documenting` for the **final-docs** pass on
the squashed head before in_review picks up; `_handle_documenting`
advances straight to `in_review`.
In_review reacts to PR state (merged/closed) and hands fresh PR
feedback (any of the four comment surfaces) off to the `fixing` stage
by recording pending-fix metadata in pinned state and flipping the
label -- no debounce wait, no dev spawn from in_review itself. The
orchestrator never merges from in_review: humans drive the merge. A
mergeable PR whose current head completed the reviewer-approved
final-docs handoff (or carries a real GitHub APPROVED review) and has
no standing CHANGES_REQUESTED earns a one-shot HITL ping per head SHA;
an unmergeable PR parks awaiting human attention. Other labels are
observed and logged as not-yet-implemented.
"""
from __future__ import annotations

import contextlib
import logging
import subprocess  # noqa: F401 -- re-exported so tests can `patch.object(workflow.subprocess, "run", ...)`
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from github.Issue import Issue

from . import analytics, config
from .agents import AgentResult, run_agent
from .config import RepoSpec
from .github import (
    BACKLOG_LABEL,
    GitHubClient,
    PinnedState,
    issue_has_label,
)
from .scheduler import IssueScheduler

# Compatibility facade: `workflow.py` keeps the dispatcher, the tick loop,
# the unlabeled-pickup handler, and `_park_awaiting_human` / `_run_agent_tracked`.
# Everything else lives in helper modules (`workflow_drift`, `workflow_messages`,
# `worktrees`) or in the per-stage modules under `orchestrator.stages`.
#
# The re-export blocks below republish those names on `workflow.<name>` so two
# call patterns keep working without touching the helper modules:
#   1. Tests patch primitives as `patch.object(workflow, "_foo", ...)`.
#   2. Stage modules reach back through `from .. import workflow as _wf` and
#      call `_wf._foo(...)` so a patch on `workflow._foo` intercepts even when
#      the call site lives in `orchestrator.stages.<stage>`.
# The redundant `as <name>` aliasing is the pyflakes/ruff convention marking
# an intentional re-export so F401 does not flag the name as unused.

from .workflow_drift import (
    _build_user_content_change_prompt as _build_user_content_change_prompt,
)
from .workflow_drift import _compute_user_content_hash as _compute_user_content_hash
from .workflow_drift import (
    _detect_user_content_change as _detect_user_content_change,
)
from .workflow_drift import (
    _mark_drift_comments_consumed as _mark_drift_comments_consumed,
)
from .workflow_drift import _route_drift_to_decomposing as _route_drift_to_decomposing
from .workflow_messages import _orchestrator_ids, _post_issue_comment
from .workflow_messages import _MANIFEST_RE as _MANIFEST_RE
from .workflow_messages import _ORCH_COMMENT_MARKER as _ORCH_COMMENT_MARKER
from .workflow_messages import _STDERR_TAIL_BUDGET as _STDERR_TAIL_BUDGET
from .workflow_messages import (
    _build_conflict_resolution_prompt as _build_conflict_resolution_prompt,
)
from .workflow_messages import _build_decompose_prompt as _build_decompose_prompt
from .workflow_messages import (
    _build_documentation_prompt as _build_documentation_prompt,
)
from .workflow_messages import _build_fix_prompt as _build_fix_prompt
from .workflow_messages import _build_implement_prompt as _build_implement_prompt
from .workflow_messages import (
    _build_pr_comment_followup as _build_pr_comment_followup,
)
from .workflow_messages import (
    _build_question_followup_prompt as _build_question_followup_prompt,
)
from .workflow_messages import _build_question_prompt as _build_question_prompt
from .workflow_messages import _build_review_prompt as _build_review_prompt
from .workflow_messages import _drift_ack_reason as _drift_ack_reason
from .workflow_messages import (
    _format_stderr_diagnostics as _format_stderr_diagnostics,
)
from .workflow_messages import (
    _parse_documentation_verdict as _parse_documentation_verdict,
)
from .workflow_messages import _parse_manifest as _parse_manifest
from .workflow_messages import _parse_review_verdict as _parse_review_verdict
from .workflow_messages import _post_pr_comment as _post_pr_comment
from .workflow_messages import _recent_comments_text as _recent_comments_text
from .workflow_messages import _redact_secrets as _redact_secrets
from .workflow_messages import _stderr_log_tail as _stderr_log_tail
from .workflow_messages import _with_orch_marker as _with_orch_marker
from .worktrees import _authed_fetch as _authed_fetch
from .worktrees import _authed_target_fetch as _authed_target_fetch
from .worktrees import _branch_ahead_behind as _branch_ahead_behind
from .worktrees import (
    _branch_has_unpushed_commits as _branch_has_unpushed_commits,
)
from .worktrees import _branch_name as _branch_name
from .worktrees import _cleanup_decompose_worktree as _cleanup_decompose_worktree
from .worktrees import _cleanup_question_worktree as _cleanup_question_worktree
from .worktrees import _cleanup_terminal_branch as _cleanup_terminal_branch
from .worktrees import _decompose_worktree_path as _decompose_worktree_path
from .worktrees import _ensure_decompose_worktree as _ensure_decompose_worktree
from .worktrees import _ensure_pr_worktree as _ensure_pr_worktree
from .worktrees import _ensure_worktree as _ensure_worktree
from .worktrees import _first_commit_subject as _first_commit_subject
from .worktrees import _git as _git
from .worktrees import _git_hardened as _git_hardened
from .worktrees import _has_new_commits as _has_new_commits
from .worktrees import _head_sha as _head_sha
from .worktrees import _is_conventional_subject as _is_conventional_subject
# TODO(remove after 2026-08-24): remove this compatibility re-export with
# worktrees._merge_base_into_worktree.
from .worktrees import _merge_base_into_worktree as _merge_base_into_worktree
from .worktrees import _rebase_base_into_worktree as _rebase_base_into_worktree
from .worktrees import (
    _pr_title_from_commit_or_issue as _pr_title_from_commit_or_issue,
)
from .worktrees import _push_branch as _push_branch
from .worktrees import _refresh_base_and_worktrees as _refresh_base_and_worktrees
from .worktrees import _rebase_in_progress as _rebase_in_progress
from .worktrees import _run_verify_commands as _run_verify_commands
from .worktrees import _sanitize_slug as _sanitize_slug
from .worktrees import _squash_and_force_push as _squash_and_force_push
from .worktrees import _sync_worktree_with_base as _sync_worktree_with_base
from .worktrees import _worktree_dirty_files as _worktree_dirty_files
from .worktrees import _worktree_path as _worktree_path
from .stages.conflicts import (
    _handle_resolving_conflict as _handle_resolving_conflict,
)
from .stages.decomposition import _handle_blocked as _handle_blocked
from .stages.decomposition import _handle_decomposing as _handle_decomposing
from .stages.decomposition import _handle_ready as _handle_ready
from .stages.decomposition import _handle_umbrella as _handle_umbrella
from .stages.decomposition import _read_decomposer_session as _read_decomposer_session
from .stages.documenting import _handle_documenting as _handle_documenting
from .stages.fixing import _handle_fixing as _handle_fixing
from .stages.implementing import (
    _SILENT_PARKS_BEFORE_FRESH_SESSION as _SILENT_PARKS_BEFORE_FRESH_SESSION,
)
from .stages.implementing import (
    _check_and_increment_retry_budget as _check_and_increment_retry_budget,
)
from .stages.implementing import _handle_implementing as _handle_implementing
from .stages.implementing import (
    _is_stale_session_failure as _is_stale_session_failure,
)
from .stages.implementing import _on_dirty_worktree as _on_dirty_worktree
from .stages.implementing import _on_question as _on_question
from .stages.implementing import _read_dev_session as _read_dev_session
from .stages.implementing import _resume_dev_with_text as _resume_dev_with_text
from .stages.implementing import (
    _resume_developer_on_human_reply as _resume_developer_on_human_reply,
)
from .stages.in_review import _comment_created_at as _comment_created_at
from .stages.in_review import _handle_in_review as _handle_in_review
from .stages.question import _handle_question as _handle_question
from .stages.validating import _handle_dev_fix_result as _handle_dev_fix_result
from .stages.validating import _handle_validating as _handle_validating
from .stages.validating import _latest_pr_comment_ids as _latest_pr_comment_ids
from .stages.validating import (
    _post_user_content_change_result as _post_user_content_change_result,
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
# blocking unrelated implementing / documenting / validating issues
# on the same tick. Stages outside this set (`ready`, `implementing`,
# `documenting`, `validating`, `in_review`, `fixing`,
# `resolving_conflict`, `question`) only read and write their own
# per-issue state + worktree, so they stay eligible for
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
    agent_spec: Optional[str] = None,
    resume_session_id: Optional[str] = None,
    timeout: Optional[int] = None,
    extra_args: tuple[str, ...] = (),
    review_round: Optional[int] = None,
    retry_count: Optional[int] = None,
) -> AgentResult:
    """Run an agent, bookending the spawn with `agent_spawn` / `agent_exit`
    audit events and appending a per-invocation analytics record on exit.

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

    After the audit `agent_exit` is emitted, an analytics record is
    appended to `analytics.ANALYTICS_LOG_PATH` via `analytics.append_record`
    (a no-op when the sink is disabled). The record carries the same
    contextual fields (`repo`, `issue`, `stage`, `agent_role`, `backend`,
    `agent_spec`, `resume_session_id` / `session_id`, `review_round`,
    `retry_count`, `duration_s`, `exit_code`, `timed_out`) plus parsed
    token counts, model list, `cost_usd`, and `cost_source` extracted
    from `result.stdout` by `usage.parse_agent_usage`. The configured
    model is pulled out of `extra_args` (via `_configured_model`) and
    passed as the parser's `fallback_model` so a codex run whose stdout
    omits the model name still records the configured model and an
    estimated cost when the SKU is in the price table. Prompts, raw
    stdout/stderr, secrets, and worktree contents are intentionally NOT
    stored -- the sink is a foundation for usage / cost aggregation, not
    a debugging mirror, and `result.stdout` may contain user-issue text.
    A parser failure or a sink IO error is swallowed so an analytics
    misconfiguration cannot stop the per-issue tick.
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
    analytics.record_agent_exit(
        repo=getattr(gh, "_repo_slug", None) or "",
        issue=issue_number,
        stage=stage,
        agent_role=agent_role,
        backend=backend,
        agent_spec=agent_spec,
        resume_session_id=resume_session_id,
        result=result,
        duration_s=duration_s,
        review_round=review_round,
        retry_count=retry_count,
        fallback_model=_configured_model(backend, extra_args),
    )
    return result


def _configured_model(
    backend: str, extra_args: tuple[str, ...]
) -> Optional[str]:
    """Pull the configured model name out of a backend's `extra_args`.

    codex selects the model with `-m <model>` (or `-m=<model>`); claude
    uses `--model <model>` (or `--model=<model>`). Whichever is present
    is forwarded to `usage.parse_agent_usage` as `fallback_model` so a
    codex run whose stdout carries usage frames but omits the model
    (resume frames, minimal completions, schema drift) still produces a
    populated `models` list and -- when the model is in the price table
    -- an estimated `cost_usd`. Returns `None` when neither flag is
    set so the parser keeps its own "unknown" handling.

    The split-form (`-m gpt-5`) and `=`-form (`--model=gpt-5`) are both
    accepted because `shlex.split` produces either shape depending on
    the operator's quoting; only one needs to win.
    """
    flag = "-m" if backend == "codex" else "--model"
    eq_prefix = flag + "="
    for i, tok in enumerate(extra_args):
        if tok == flag and i + 1 < len(extra_args):
            value = extra_args[i + 1].strip()
            return value or None
        if tok.startswith(eq_prefix):
            value = tok[len(eq_prefix):].strip()
            return value or None
    return None


def tick(
    gh: GitHubClient,
    spec: RepoSpec,
    *,
    global_semaphore: Optional[threading.BoundedSemaphore] = None,
    scheduler: Optional[IssueScheduler] = None,
) -> None:
    """Drive a single tick for one repo.

    `global_semaphore` is the cross-repo bound on concurrent per-issue
    handlers (`MAX_PARALLEL_ISSUES_GLOBAL`). It is acquired around every
    `_process_issue` call so workers from different repo ticks running
    concurrently contend on the same semaphore. None falls back to a
    no-op context manager so direct test invocations of `tick(gh, spec)`
    keep working unchanged; production code threads the shared semaphore
    in from `main._run_tick` so the cap is actually enforced.

    `scheduler`, when supplied, takes over per-issue dispatch entirely.
    The polling pass still refreshes base/worktrees and enumerates
    pollable issues, but instead of running the handlers in-tick (legacy
    in-thread loop or per-tick ThreadPoolExecutor) each accepted
    per-issue callable is submitted to the scheduler and the tick
    returns without waiting for completion. The scheduler owns the
    cross-repo in-flight cap, the per-repo cap (`spec.parallel_limit`
    is threaded in as the per-call override), the "duplicate active
    issue" skip, and the family-aware mutex. `global_semaphore` is
    ignored on this path -- the scheduler's `global_cap` is the
    authoritative cross-repo bound. None preserves the legacy in-tick
    behavior so existing direct invocations are unchanged.
    """
    try:
        # Threading the scheduler in here is what keeps an "active
        # issue" actually inert across the whole tick. The dispatch
        # path skips a duplicate submit at `scheduler.submit`, but the
        # base refresh would otherwise rebase the pre-PR worktree
        # under a still-running agent or relabel/state-mutate a
        # PR-having worktree while its handler is mid-write. The
        # refresh helper consults `scheduler.is_active` per worktree
        # so an in-flight issue's worktree and pinned state are left
        # alone until the worker exits.
        _refresh_base_and_worktrees(gh, spec, scheduler=scheduler)
    except Exception:
        log.exception(
            "repo=%s pre-tick base refresh failed; continuing", spec.slug,
        )
    if scheduler is not None:
        _dispatch_via_scheduler(gh, spec, scheduler)
        return
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
    # `parallel_limit` is the local cap on worker threads this tick will
    # spin up. The host-wide `MAX_PARALLEL_ISSUES_GLOBAL` cap is enforced
    # by `global_semaphore` around each `_process_issue` call, not by
    # shrinking the worker pool: with multiple repos ticking in parallel,
    # workers from different repos may queue on the semaphore until a
    # global slot frees up, which is the whole point of a cross-repo cap.
    # Shrinking the pool here would mean a single quiet repo could
    # under-utilize the budget even when other repos have nothing to do.
    limit = max(1, int(getattr(spec, "parallel_limit", 1) or 1))
    semaphore_cm = (
        global_semaphore if global_semaphore is not None else contextlib.nullcontext()
    )
    if limit == 1:
        for issue in gh.list_pollable_issues():
            try:
                with semaphore_cm:
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
                with semaphore_cm:
                    _process_issue(worker_gh, spec, worker_issue)
            except Exception:
                log.exception(
                    "repo=%s issue=#%s processing failed",
                    spec.slug, issue_number,
                )

    def _run_fanout_in_worker(issue_number: int) -> None:
        worker_gh = gh._for_worker_thread()
        worker_issue = worker_gh.get_issue(issue_number)
        with semaphore_cm:
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


def _dispatch_via_scheduler(
    gh: GitHubClient, spec: RepoSpec, scheduler: IssueScheduler,
) -> None:
    """Enumerate pollable issues this tick and hand each one to the scheduler.

    Classifies family-aware work (unlabeled pickup + decomposing /
    blocked / umbrella -- the cross-issue writers) versus per-issue
    fan-out work, then submits an accepted per-issue callable for each
    one. The submit path is nonblocking: a duplicate (already in-flight)
    issue, a global / per-repo cap hit, or a family slot already held
    by another worker is simply skipped this tick; the next polling
    pass re-enumerates and retries against the live scheduler state.

    The per-issue callable mirrors the legacy parallel path: it mints a
    fresh `GitHubClient` via `gh._for_worker_thread()` and refetches the
    Issue with that client so the worker drives its own Requester chain
    (PyGithub is not documented thread-safe). A scheduler.reap() at the
    end of the dispatch loop drains any completions that landed during
    enumeration so worker failures surface in the log on the tick that
    they fired, not the next one.

    `spec.parallel_limit` is forwarded as the scheduler's per-call
    cap override so a per-repo configuration tighter than the
    scheduler default still binds. Label-read failures route the
    offending issue into the family bucket so `_process_issue`'s
    own exception isolation can pick up any sustained failure -- the
    same recovery the legacy parallel path uses.
    """
    per_repo_cap = max(1, int(getattr(spec, "parallel_limit", 1) or 1))
    for issue in gh.list_pollable_issues():
        try:
            label = gh.workflow_label(issue)
        except Exception:
            log.exception(
                "repo=%s issue=#%s workflow_label read failed; routing to "
                "family bucket so per-issue exception isolation can pick "
                "up any sustained failure", spec.slug, issue.number,
            )
            label = None
        family = label is None or label in _FAMILY_AWARE_LABELS
        issue_number = int(issue.number)

        def _run(number: int = issue_number) -> None:
            worker_gh = gh._for_worker_thread()
            worker_issue = worker_gh.get_issue(number)
            _process_issue(worker_gh, spec, worker_issue)

        scheduler.submit(
            spec.slug,
            issue_number,
            _run,
            family=family,
            per_repo_cap=per_repo_cap,
        )
    # Drain any completions that landed during enumeration so worker
    # failures are logged on the tick that produced them, not the next.
    scheduler.reap()


def _process_issue(gh: GitHubClient, spec: RepoSpec, issue: Issue) -> None:
    # Postponed-task hold: applying `backlog` parks the issue outside the
    # state machine entirely until the label is removed. Checked before
    # reading the workflow label so the orchestrator never decomposes,
    # spawns an agent, or otherwise reacts while the operator is using
    # the label as a "not yet" signal. Backlog-skips are NOT counted as a
    # stage evaluation: no handler runs and there is nothing to time.
    if issue_has_label(issue, BACKLOG_LABEL):
        log.info(
            "repo=%s issue=#%s has %r; skipping",
            spec.slug, issue.number, BACKLOG_LABEL,
        )
        return
    label = gh.workflow_label(issue)
    log.info("repo=%s issue=#%s label=%r", spec.slug, issue.number, label)
    # Time the handler dispatch and append a single `stage_evaluation`
    # analytics record on exit. `result` flips to "error" inside the
    # except clause so an unhandled exception still produces a timing
    # record before propagating -- the tick loop's per-issue try/except
    # already logs and isolates the failure, so re-raising here keeps
    # the existing dispatch / exception contract intact. The append
    # itself is internally hardened against OSError; an analytics
    # misconfiguration cannot stop the per-issue tick from advancing.
    start = time.monotonic()
    result = "ok"
    try:
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
        elif label == "documenting":
            _handle_documenting(gh, spec, issue)
        elif label == "validating":
            _handle_validating(gh, spec, issue)
        elif label == "in_review":
            _handle_in_review(gh, spec, issue)
        elif label == "fixing":
            _handle_fixing(gh, spec, issue)
        elif label == "resolving_conflict":
            _handle_resolving_conflict(gh, spec, issue)
        elif label == "question":
            _handle_question(gh, spec, issue)
        elif label in ("done", "rejected"):
            return
        else:
            log.warning(
                "repo=%s issue=#%s label=%r not implemented yet; leaving alone",
                spec.slug, issue.number, label,
            )
    except Exception:
        result = "error"
        raise
    finally:
        duration_s = round(time.monotonic() - start, 3)
        analytics.record_stage_evaluation(
            repo=getattr(gh, "_repo_slug", None) or "",
            issue=issue.number,
            stage=label,
            duration_s=duration_s,
            result=result,
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
    `park_reason` -- a transient park (e.g. in_review `unmergeable`)
    followed by a follow-up question/timeout park would otherwise leave
    the transient reason behind. Callers that re-park for a transient
    reason re-set `park_reason` immediately after this call.

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


def _finalize_if_pr_merged(
    gh: GitHubClient, spec: RepoSpec, issue: Issue, state: PinnedState,
) -> bool:
    """Flip the issue to `done` when its linked PR has already merged.

    Mirrors the terminal-merge arc in `_handle_in_review` / `_handle_fixing`
    / `_handle_resolving_conflict` so the same finalize path can fire from
    any stage. Used by handlers that previously had no merged-PR check
    (`_handle_implementing`, `_handle_documenting`, `_handle_validating`)
    and by the umbrella / blocked aggregation when a child PR was merged
    externally but the child's workflow label was never advanced past the
    in-flight stage -- the umbrella's all-`done` aggregation would
    otherwise wait forever for that stale child.

    Returns True when the helper finalized the issue (caller must return
    immediately); False when there is nothing to do (no `pr_number`, PR
    fetch failed, or PR is not merged).
    """
    pr_number = state.get("pr_number")
    if pr_number is None:
        return False
    try:
        pr = gh.get_pr(int(pr_number))
    except Exception:
        log.exception(
            "issue=#%s could not fetch PR #%s while checking for "
            "external merge; leaving alone", issue.number, pr_number,
        )
        return False
    if gh.pr_state(pr) != "merged":
        return False
    stage = gh.workflow_label(issue)
    state.set("merged_at", _now_iso())
    gh.set_workflow_label(issue, "done")
    gh.write_pinned_state(issue, state)
    gh.emit_event(
        "pr_merged",
        issue_number=issue.number,
        stage=stage,
        pr_number=int(pr_number),
        sha=getattr(pr.head, "sha", None) or None,
        merge_method="external",
        review_round=int(state.get("review_round") or 0),
        conflict_round=state.get("conflict_round"),
        retry_count=state.get("retry_count"),
    )
    if getattr(issue, "state", "open") != "closed":
        try:
            issue.edit(state="closed")
        except Exception:
            log.exception(
                "issue=#%s could not close after detecting external merge",
                issue.number,
            )
    _cleanup_terminal_branch(gh, spec, issue.number)
    return True


def _drain_review_pr_terminals(
    gh: GitHubClient,
    spec: RepoSpec,
    issue: Issue,
    state: PinnedState,
    pr,
    *,
    stage: str,
) -> bool:
    """Drain the three PR/issue terminal arcs shared by `_handle_in_review`,
    `_handle_fixing`, and `_handle_resolving_conflict`.

    Caller passes the already-fetched PR and its own `stage` label. Each
    stage owns its fetch-failure semantics: `in_review` and
    `resolving_conflict` let `gh.get_pr` exceptions propagate to
    `_process_issue`'s catch; `fixing` catches and bails with `pr=None`
    so the rest of its handler can short-circuit. Passing `pr=None` here
    is a no-op (returns False) so fixing's deferral arrives unchanged.

    Three arcs (mirrors the original inline code in each stage):

      1. `pr_state == "merged"`: stamp `merged_at`, flip to `done`,
         write state, emit `pr_merged` (`merge_method="external"`),
         close the issue if still open, and clean up the branch.
      2. `pr_state == "closed"` (unmerged): stamp
         `closed_without_merge_at`, flip to `rejected`, write state,
         emit `pr_closed_without_merge`, close the issue if still open,
         and clean up the branch.
      3. Issue is closed but PR is still open (the closed-issue sweep
         surfaced a human stop signal): stamp `closed_without_merge_at`,
         flip to `rejected`, write state. Deliberately no event emit
         (the PR is still open and may be reopened/salvaged) and no
         branch cleanup (the operator may want the open PR's history).

    Returns True when an arc fired (caller must return immediately).
    Returns False when none fired (caller continues with the same `pr`).
    """
    if pr is None:
        return False
    pr_number = int(state.get("pr_number"))
    pr_status = gh.pr_state(pr)
    # `resolving_conflict` terminal events historically coerced
    # `conflict_round` via `int(state.get("conflict_round") or 0)` so a
    # legacy / manually-relabelled state without the counter still landed
    # `0` in the audit record. `build_event_record` drops None-valued
    # kwargs, so without this coercion those legacy states would lose the
    # field entirely. The other two stages have always emitted the raw
    # `state.get("conflict_round")` (so a missing counter stays missing),
    # so the stage-conditional coercion preserves both pre-refactor
    # behaviours exactly.
    conflict_round_field = state.get("conflict_round")
    if stage == "resolving_conflict":
        conflict_round_field = int(conflict_round_field or 0)
    if pr_status == "merged":
        state.set("merged_at", _now_iso())
        gh.set_workflow_label(issue, "done")
        gh.write_pinned_state(issue, state)
        gh.emit_event(
            "pr_merged",
            issue_number=issue.number,
            stage=stage,
            pr_number=pr_number,
            sha=getattr(pr.head, "sha", None) or None,
            merge_method="external",
            review_round=int(state.get("review_round") or 0),
            conflict_round=conflict_round_field,
            retry_count=state.get("retry_count"),
        )
        try:
            issue.edit(state="closed")
        except Exception:
            log.exception(
                "issue=#%s could not close after merge", issue.number,
            )
        _cleanup_terminal_branch(gh, spec, issue.number)
        return True
    if pr_status == "closed":
        state.set("closed_without_merge_at", _now_iso())
        gh.set_workflow_label(issue, "rejected")
        gh.write_pinned_state(issue, state)
        gh.emit_event(
            "pr_closed_without_merge",
            issue_number=issue.number,
            stage=stage,
            pr_number=pr_number,
            sha=getattr(pr.head, "sha", None) or None,
            review_round=int(state.get("review_round") or 0),
            conflict_round=conflict_round_field,
            retry_count=state.get("retry_count"),
        )
        try:
            issue.edit(state="closed")
        except Exception:
            log.exception(
                "issue=#%s could not close after reject", issue.number,
            )
        _cleanup_terminal_branch(gh, spec, issue.number)
        return True
    if getattr(issue, "state", "open") == "closed":
        state.set("closed_without_merge_at", _now_iso())
        gh.set_workflow_label(issue, "rejected")
        gh.write_pinned_state(issue, state)
        return True
    return False


def _finalize_if_issue_closed(
    gh: GitHubClient, spec: RepoSpec, issue: Issue, state: PinnedState,
) -> bool:
    """Flip a closed-but-not-merged issue to `rejected`.

    Pairs with `_finalize_if_pr_merged`: that helper drains the merged-PR
    arc, this one drains the closed-issue counterpart so closed issues
    yielded by the new `implementing` / `documenting` / `validating`
    sweep entries do NOT spawn the dev / docs / reviewer agent, push to
    the per-issue branch, or post on the now-closed issue thread.
    `_handle_in_review` / `_handle_fixing` carry equivalent guards
    inline via their PR-state arcs; callers in the new sweep stages
    invoke this helper right after `_finalize_if_pr_merged` so the
    merged case is drained first and only the rejected case lands here.

    Branch cleanup follows the in_review / fixing convention: only when
    the linked PR itself is also closed (a closed PR without merge is
    `pr_closed_without_merge`-emit territory and the branch is dead
    weight). An open PR with a manually-closed issue is left alone so
    the operator can salvage / reopen it; the orchestrator-owned branch
    and worktree stay until the PR closes.

    Returns True when the caller must NOT continue the handler this
    tick: the issue was finalized to `rejected`, OR the issue is closed
    but the linked PR state could not be confirmed yet (deferred to a
    later tick so a transient fetch failure cannot permanently mis-
    label a merged-PR issue, AND so the closed issue is not driven
    through normal dev / docs / reviewer work). Returns False only
    when the issue is still open and the handler should proceed.
    """
    if getattr(issue, "state", "open") != "closed":
        return False
    pr_number = state.get("pr_number")
    pr = None
    if pr_number is not None:
        # Read the PR state BEFORE mutating issue state.
        # `_finalize_if_pr_merged` ran first and returned False, but
        # that helper returns False on BOTH "not merged" AND "could
        # not fetch PR" -- the two are indistinguishable to the
        # caller. Flipping to `rejected` without our own successful
        # fetch could permanently terminal-label a merged-PR issue
        # whose merge finalize hit a transient GitHub / network
        # failure. Defer instead so the next tick's
        # `_finalize_if_pr_merged` can re-attempt the merged path
        # against a fresh PR state -- but still return True so the
        # caller does NOT continue spawning the dev / docs / reviewer
        # agent against an issue that is already closed.
        try:
            pr = gh.get_pr(int(pr_number))
        except Exception:
            log.exception(
                "issue=#%s could not fetch PR #%s while finalizing a "
                "closed issue; deferring (next tick retries the "
                "merged-PR path)", issue.number, pr_number,
            )
            return True
        if gh.pr_state(pr) == "merged":
            # Our fetch succeeded and the PR IS merged -- the prior
            # `_finalize_if_pr_merged` call hit a transient fetch
            # failure of its own. Defer so the next tick runs the
            # full merged-path cleanup (stamp `merged_at`, flip to
            # `done`, emit `pr_merged` with `merge_method="external"`,
            # cleanup branch). Flipping to `rejected` here would
            # permanently mis-label this issue; returning True
            # without state changes keeps the dev / docs / reviewer
            # agent from running against a closed issue this tick.
            return True
    stage = gh.workflow_label(issue)
    state.set("closed_without_merge_at", _now_iso())
    gh.set_workflow_label(issue, "rejected")
    gh.write_pinned_state(issue, state)
    if pr is None:
        return True
    if gh.pr_state(pr) != "closed":
        # Open PR + closed issue: do NOT emit `pr_closed_without_merge`
        # (the PR is still open and may be reopened / salvaged) and do
        # NOT clean up the branch. Mirrors the in_review / fixing
        # open-PR + closed-issue arc.
        return True
    gh.emit_event(
        "pr_closed_without_merge",
        issue_number=issue.number,
        stage=stage,
        pr_number=int(pr_number),
        sha=getattr(pr.head, "sha", None) or None,
        review_round=int(state.get("review_round") or 0),
        conflict_round=state.get("conflict_round"),
        retry_count=state.get("retry_count"),
    )
    _cleanup_terminal_branch(gh, spec, issue.number)
    return True
