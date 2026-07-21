# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""State machine: drive issues through the orchestrator workflow.

(no label) -> implementing -> validating -> documenting (final-docs)
-> in_review -> done|rejected.
After the implementer commits and the PR opens, `_on_commits` relabels
straight to `validating` -- the docs pass only runs as the final-docs
handoff after the reviewer approves, not as a pre-review hop. Validating
then runs a fresh reviewer session; on CHANGES_REQUESTED the handler
relabels to `fixing` BEFORE spawning the dev so the dev-fix subphase
is observably labeled `fixing` rather than `validating`. After the
fix pushes the label flips back to `validating` (with `review_round`
bumped) and the reviewer reruns until APPROVED or MAX_REVIEW_ROUNDS
is hit -- the single docs pass is deferred to the final-docs handoff
after reviewer approval. On a parked dev fix the issue stays on
`fixing` and `_handle_fixing` owns the awaiting-human cycle from
there. After approval (+ verify + squash) the validating handler
relabels to `documenting` for the **final-docs** pass on the squashed
head before in_review picks up; `_handle_documenting` advances
straight to `in_review`.
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
import functools
import logging
import subprocess  # noqa: F401 -- re-exported so tests can `patch.object(workflow.subprocess, "run", ...)`
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping, Optional

from github.Issue import Issue

from orchestrator import analytics, config
from orchestrator.agents import AgentResult, run_agent
from orchestrator.usage import UsageMetrics
from orchestrator.state_machine import WorkflowLabel
from orchestrator.github import (
    COMMUNITY_CONTRIBUTION_LABEL,
    GitHubClient,
    PinnedState,
    hard_skip_control_label,
)
from orchestrator.scheduler import IssueScheduler

# Compatibility facade: `workflow.py` keeps the dispatcher, the tick loop,
# the unlabeled-pickup handler, and `_park_awaiting_human` / `_run_agent_tracked`.
# Everything else lives in helper modules (`workflow_drift`, `workflow_messages`,
# `worktrees`) or in the per-stage modules under `orchestrator.stages`.
#
# The re-export blocks below republish those names on `workflow.<name>` so two
# call patterns keep working without touching the helper modules:
#   1. Tests patch primitives as `patch.object(workflow, "_foo", ...)`.
#   2. Stage modules reach back through `from orchestrator import workflow as _wf`
#      and call `_wf._foo(...)` so a patch on `workflow._foo` intercepts even when
#      the call site lives in `orchestrator.stages.<stage>`.
# The redundant `as <name>` aliasing is the pyflakes/ruff convention marking
# an intentional re-export so F401 does not flag the name as unused.

from orchestrator.workflow_drift import (
    _build_user_content_change_prompt as _build_user_content_change_prompt,
)
from orchestrator.workflow_drift import _compute_user_content_hash as _compute_user_content_hash
from orchestrator.workflow_drift import (
    _detect_user_content_change as _detect_user_content_change,
)
from orchestrator.workflow_drift import (
    _mark_drift_comments_consumed as _mark_drift_comments_consumed,
)
from orchestrator.workflow_drift import _route_drift_to_decomposing as _route_drift_to_decomposing
from orchestrator.workflow_messages import _orchestrator_ids, _post_issue_comment
from orchestrator.workflow_messages import _MANIFEST_RE as _MANIFEST_RE
from orchestrator.workflow_messages import _FOREGROUND_ONLY_NOTE as _FOREGROUND_ONLY_NOTE
from orchestrator.workflow_messages import _ORCH_COMMENT_MARKER as _ORCH_COMMENT_MARKER
from orchestrator.workflow_messages import _STDERR_TAIL_BUDGET as _STDERR_TAIL_BUDGET
from orchestrator.workflow_messages import (
    _build_conflict_resolution_prompt as _build_conflict_resolution_prompt,
)
from orchestrator.workflow_messages import _build_decompose_prompt as _build_decompose_prompt
from orchestrator.workflow_messages import (
    _build_documentation_prompt as _build_documentation_prompt,
)
from orchestrator.workflow_messages import _build_fix_prompt as _build_fix_prompt
from orchestrator.workflow_messages import _build_implement_prompt as _build_implement_prompt
from orchestrator.workflow_messages import (
    _build_fresh_respawn_preamble as _build_fresh_respawn_preamble,
)
from orchestrator.workflow_messages import (
    _build_pr_comment_followup as _build_pr_comment_followup,
)
from orchestrator.workflow_messages import (
    _build_question_followup_prompt as _build_question_followup_prompt,
)
from orchestrator.workflow_messages import _build_question_prompt as _build_question_prompt
from orchestrator.workflow_messages import _build_review_prompt as _build_review_prompt
from orchestrator.workflow_messages import (
    _build_single_decision_comment as _build_single_decision_comment,
)
from orchestrator.workflow_messages import (
    _build_tracked_repos_context as _build_tracked_repos_context,
)
from orchestrator.workflow_messages import _drift_ack_reason as _drift_ack_reason
from orchestrator.workflow_messages import _CONTINUE_PARK_REASONS as _CONTINUE_PARK_REASONS
from orchestrator.workflow_messages import _CONTINUE_RETRY_PROMPT as _CONTINUE_RETRY_PROMPT
from orchestrator.workflow_messages import (
    _parse_orchestrator_continue as _parse_orchestrator_continue,
)
from orchestrator.workflow_messages import (
    _is_bare_orchestrator_continue as _is_bare_orchestrator_continue,
)
from orchestrator.workflow_messages import (
    _continue_command_action as _continue_command_action,
)
from orchestrator.workflow_messages import (
    _refuse_parked_continue as _refuse_parked_continue,
)
from orchestrator.workflow_messages import (
    _format_stderr_diagnostics as _format_stderr_diagnostics,
)
from orchestrator.workflow_messages import (
    _parse_documentation_verdict as _parse_documentation_verdict,
)
from orchestrator.workflow_messages import _parse_manifest as _parse_manifest
from orchestrator.workflow_messages import _parse_review_verdict as _parse_review_verdict
from orchestrator.workflow_messages import _post_pr_comment as _post_pr_comment
from orchestrator.workflow_messages import _as_blockquote as _as_blockquote
from orchestrator.workflow_messages import _quote_comment_line as _quote_comment_line
from orchestrator.workflow_messages import _recent_comments_text as _recent_comments_text
from orchestrator.workflow_messages import _redact_secrets as _redact_secrets
from orchestrator.workflow_messages import _stderr_log_tail as _stderr_log_tail
from orchestrator.workflow_messages import _with_orch_marker as _with_orch_marker
from orchestrator.base_sync import (
    _AUTO_REBASE_PARK_REASONS as _AUTO_REBASE_PARK_REASONS,
)
from orchestrator.base_sync import (
    _refresh_base_and_worktrees as _refresh_base_and_worktrees,
)
from orchestrator.base_sync import _rebase_base_into_worktree as _rebase_base_into_worktree
from orchestrator.base_sync import _rebase_in_progress as _rebase_in_progress
from orchestrator.base_sync import (
    _sync_pr_worktree_to_base as _sync_pr_worktree_to_base,
)
from orchestrator.base_sync import _sync_worktree_with_base as _sync_worktree_with_base
# TODO(remove after 2026-08-24): remove this compatibility re-export with
# base_sync._merge_base_into_worktree.
from orchestrator.base_sync import _merge_base_into_worktree as _merge_base_into_worktree
from orchestrator.skill_catalog import _emit_repo_skill_catalog as _emit_repo_skill_catalog
from orchestrator.worktrees import _authed_fetch as _authed_fetch
from orchestrator.worktrees import _authed_target_fetch as _authed_target_fetch
from orchestrator.worktrees import _branch_ahead_behind as _branch_ahead_behind
from orchestrator.worktrees import (
    _branch_has_unpushed_commits as _branch_has_unpushed_commits,
)
from orchestrator.worktrees import _branch_name as _branch_name
from orchestrator.worktrees import _cleanup_decompose_worktree as _cleanup_decompose_worktree
from orchestrator.worktrees import _cleanup_question_worktree as _cleanup_question_worktree
from orchestrator.worktrees import _cleanup_terminal_branch as _cleanup_terminal_branch
from orchestrator.worktrees import _decompose_worktree_path as _decompose_worktree_path
from orchestrator.worktrees import _ensure_decompose_worktree as _ensure_decompose_worktree
from orchestrator.worktrees import _ensure_pr_worktree as _ensure_pr_worktree
from orchestrator.worktrees import _ensure_worktree as _ensure_worktree
from orchestrator.worktrees import _first_commit_subject as _first_commit_subject
from orchestrator.worktrees import _git as _git
from orchestrator.worktrees import _git_hardened as _git_hardened
from orchestrator.worktrees import _has_new_commits as _has_new_commits
from orchestrator.worktrees import _head_sha as _head_sha
from orchestrator.worktrees import _infer_subject_prefix as _infer_subject_prefix
from orchestrator.worktrees import _is_conventional_subject as _is_conventional_subject
from orchestrator.worktrees import _is_prefixed_subject as _is_prefixed_subject
from orchestrator.worktrees import (
    _pr_title_from_commit_or_issue as _pr_title_from_commit_or_issue,
)
from orchestrator.worktrees import _push_branch as _push_branch
from orchestrator.worktrees import _resolve_branch_name as _resolve_branch_name
from orchestrator.worktrees import _run_verify_commands as _run_verify_commands
from orchestrator.worktrees import _sanitize_branch_segment as _sanitize_branch_segment
from orchestrator.worktrees import _sanitize_slug as _sanitize_slug
from orchestrator.worktrees import _squash_and_force_push as _squash_and_force_push
from orchestrator.worktrees import _worktree_dirty_files as _worktree_dirty_files
from orchestrator.worktrees import _worktree_path as _worktree_path
from orchestrator.stages.conflicts import (
    _handle_resolving_conflict as _handle_resolving_conflict,
)
from orchestrator.stages.decomposition import _handle_blocked as _handle_blocked
from orchestrator.stages.decomposition import _handle_decomposing as _handle_decomposing
from orchestrator.stages.decomposition import _handle_ready as _handle_ready
from orchestrator.stages.decomposition import _handle_umbrella as _handle_umbrella
from orchestrator.stages.decomposition import _read_decomposer_session as _read_decomposer_session
from orchestrator.stages.documenting import _handle_documenting as _handle_documenting
from orchestrator.stages.fixing import _handle_fixing as _handle_fixing
from orchestrator.stages.implementing import (
    _SILENT_PARKS_BEFORE_FRESH_SESSION as _SILENT_PARKS_BEFORE_FRESH_SESSION,
)
from orchestrator.stages.implementing import (
    _check_and_increment_retry_budget as _check_and_increment_retry_budget,
)
from orchestrator.stages.implementing import _handle_implementing as _handle_implementing
from orchestrator.stages.implementing import (
    _is_stale_session_failure as _is_stale_session_failure,
)
from orchestrator.stages.implementing import (
    _is_context_overflow_failure as _is_context_overflow_failure,
)
from orchestrator.stages.implementing import (
    _is_session_limit_message as _is_session_limit_message,
)
from orchestrator.stages.implementing import (
    _drop_poisoned_dev_session as _drop_poisoned_dev_session,
)
from orchestrator.stages.implementing import (
    _is_poisoned_session_failure as _is_poisoned_session_failure,
)
from orchestrator.stages.implementing import _on_dirty_worktree as _on_dirty_worktree
from orchestrator.stages.implementing import _on_question as _on_question
from orchestrator.stages.implementing import _read_dev_session as _read_dev_session
from orchestrator.stages.implementing import _resume_dev_with_text as _resume_dev_with_text
from orchestrator.stages.implementing import (
    _resume_developer_on_human_reply as _resume_developer_on_human_reply,
)
from orchestrator.stages.in_review import _comment_created_at as _comment_created_at
from orchestrator.stages.in_review import _handle_in_review as _handle_in_review
from orchestrator.stages.question import _handle_question as _handle_question
from orchestrator.stages.validating import (
    _VALIDATING_TRANSIENT_PARK_REASONS as _VALIDATING_TRANSIENT_PARK_REASONS,
)
from orchestrator.stages.validating import _handle_dev_fix_result as _handle_dev_fix_result
from orchestrator.stages.validating import _handle_validating as _handle_validating
from orchestrator.stages.validating import _latest_pr_comment_ids as _latest_pr_comment_ids
from orchestrator.stages.validating import (
    _post_user_content_change_result as _post_user_content_change_result,
)
from orchestrator.stages.validating import _stranded_fix_unpushed as _stranded_fix_unpushed
from orchestrator.stages.validating import (
    _try_recover_validating_transient_park as _try_recover_validating_transient_park,
)

# Canonical inventory of the `workflow.<name>` surface: the dispatcher /
# tick-loop / pickup API this module defines itself (`tick`,
# `_run_agent_tracked`, `_park_awaiting_human`, the finalize helpers, ...)
# plus every helper and stage handler re-exported above from the helper and
# `orchestrator.stages` modules. The re-exports are what let two call patterns
# keep working: tests that `patch.object(workflow, "_foo", ...)` and stage
# modules that reach back through `from orchestrator import workflow as _wf`. Listing
# them here makes that large re-export surface auditable in one place and
# governs `from orchestrator.workflow import *`; the `subprocess` entry is the
# stdlib module re-exported so tests can patch `workflow.subprocess.run`.
__all__ = [
    "_AUTO_REBASE_PARK_REASONS",
    "_CAP_EXEMPT_FAMILY_LABELS",
    "_CONTINUE_PARK_REASONS",
    "_CONTINUE_RETRY_PROMPT",
    "_FAMILY_AWARE_LABELS",
    "_FAMILY_BUCKET_ISSUE",
    "_FOREGROUND_ONLY_NOTE",
    "_MANIFEST_RE",
    "_ORCH_COMMENT_MARKER",
    "_PollablePartition",
    "_SILENT_PARKS_BEFORE_FRESH_SESSION",
    "_STDERR_TAIL_BUDGET",
    "_VALIDATING_TRANSIENT_PARK_REASONS",
    "_accumulate_issue_usage",
    "_as_blockquote",
    "_authed_fetch",
    "_authed_target_fetch",
    "_branch_ahead_behind",
    "_branch_has_unpushed_commits",
    "_branch_name",
    "_build_conflict_resolution_prompt",
    "_build_decompose_prompt",
    "_build_documentation_prompt",
    "_build_fix_prompt",
    "_build_fresh_respawn_preamble",
    "_build_implement_prompt",
    "_build_pr_comment_followup",
    "_build_question_followup_prompt",
    "_build_question_prompt",
    "_build_review_prompt",
    "_build_single_decision_comment",
    "_build_tracked_repos_context",
    "_build_user_content_change_prompt",
    "_check_and_increment_retry_budget",
    "_classify_pollable_issue",
    "_cleanup_decompose_worktree",
    "_cleanup_question_worktree",
    "_cleanup_terminal_branch",
    "_comment_created_at",
    "_compute_user_content_hash",
    "_configured_model",
    "_continue_command_action",
    "_decompose_worktree_path",
    "_detect_user_content_change",
    "_dispatch_via_scheduler",
    "_drain_family_bucket",
    "_drain_review_pr_terminals",
    "_drain_scheduler_family_bucket",
    "_drift_ack_reason",
    "_drop_poisoned_dev_session",
    "_emit_repo_skill_catalog",
    "_ensure_decompose_worktree",
    "_ensure_pr_worktree",
    "_ensure_worktree",
    "_family_bucket_cap_exempt",
    "_finalize_if_issue_closed",
    "_finalize_if_pr_merged",
    "_first_commit_subject",
    "_format_issue_usage_verdict",
    "_format_stderr_diagnostics",
    "_git",
    "_git_hardened",
    "_handle_blocked",
    "_handle_decomposing",
    "_handle_dev_fix_result",
    "_handle_documenting",
    "_handle_fixing",
    "_handle_implementing",
    "_handle_in_review",
    "_handle_pickup",
    "_handle_question",
    "_handle_ready",
    "_handle_resolving_conflict",
    "_handle_umbrella",
    "_handle_validating",
    "_has_new_commits",
    "_head_sha",
    "_ignore_if_interrupted",
    "_infer_subject_prefix",
    "_is_bare_orchestrator_continue",
    "_is_context_overflow_failure",
    "_is_conventional_subject",
    "_is_poisoned_session_failure",
    "_is_prefixed_subject",
    "_is_session_limit_message",
    "_is_stale_session_failure",
    "_issue_is_closed",
    "_latest_pr_comment_ids",
    "_mark_drift_comments_consumed",
    "_merge_base_into_worktree",
    "_now_iso",
    "_on_dirty_worktree",
    "_on_question",
    "_orchestrator_ids",
    "_park_awaiting_human",
    "_parse_documentation_verdict",
    "_parse_manifest",
    "_parse_orchestrator_continue",
    "_parse_review_verdict",
    "_partition_pollable_issues",
    "_paused_during_agent_run",
    "_post_issue_comment",
    "_post_issue_usage_verdict",
    "_post_pr_comment",
    "_post_user_content_change_result",
    "_pr_title_from_commit_or_issue",
    "_process_issue",
    "_push_branch",
    "_quote_comment_line",
    "_read_decomposer_session",
    "_read_dev_session",
    "_rebase_base_into_worktree",
    "_rebase_in_progress",
    "_recent_comments_text",
    "_redact_secrets",
    "_refetch_and_process",
    "_refresh_base_and_worktrees",
    "_refuse_parked_continue",
    "_resolve_branch_name",
    "_resume_dev_with_text",
    "_resume_developer_on_human_reply",
    "_route_drift_to_decomposing",
    "_route_issue_to_handler",
    "_run_agent_tracked",
    "_run_parallel_tick",
    "_run_sequential_tick",
    "_run_verify_commands",
    "_sanitize_branch_segment",
    "_sanitize_slug",
    "_squash_and_force_push",
    "_stderr_log_tail",
    "_stranded_fix_unpushed",
    "_sweep_community_contribution_prs",
    "_sync_pr_worktree_to_base",
    "_sync_worktree_with_base",
    "_try_recover_validating_transient_park",
    "_with_orch_marker",
    "_worktree_dirty_files",
    "_worktree_path",
    "subprocess",
    "tick",
]

log = logging.getLogger(__name__)


# Workflow labels whose handlers can read or write OTHER issues' pinned
# state -- the cross-issue writers are:
#   * `_handle_decomposing` -- creates child issues, seeds their pinned
#     state, may flip their labels (`set_workflow_label(child, WorkflowLabel.READY)`),
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
_FAMILY_AWARE_LABELS = frozenset((
    WorkflowLabel.DECOMPOSING, WorkflowLabel.BLOCKED, WorkflowLabel.UMBRELLA,
))

# Family-aware labels whose stage handler is a pure GitHub label / dep-graph
# walk -- no agent spawn, no worktree mutation (`_handle_blocked`,
# `_handle_umbrella`). A family bucket made up SOLELY of these is submitted
# cap-exempt: it must never consume an agent-sized cap slot, because a
# cheap-polling parent (a `blocked` parent waiting on its own children, or an
# `umbrella` aggregating them) would otherwise be starved of the only
# per-repo slot under the default `parallel_limit=1` -- and a `blocked`
# parent waiting on its own children would deadlock the very children it
# blocks. `decomposing` is excluded (it spawns the decomposer agent), as is
# the unlabeled-pickup case (`None`, routed through `_handle_pickup`, which
# can spawn an agent too): a bucket containing either stays cap-counted.
_CAP_EXEMPT_FAMILY_LABELS = frozenset((
    "blocked", "umbrella",
))

# Shared log template for a per-issue handler that raised; the tick loop logs it
# with the repo slug and issue tag before moving on to the next issue.
_PROCESSING_FAILED_LOG = "repo=%s issue=#%s processing failed"

# GitHub issue/PR state attribute and its two values, as the dispatcher reads
# them off both PyGithub objects and the in-memory fake.
_STATE_ATTR = "state"
_ISSUE_STATE_OPEN = "open"
_ISSUE_STATE_CLOSED = "closed"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass(frozen=True)
class _AgentRunRequest:
    """Agent invocation plus the audit/analytics context that follows it."""

    agent_role: str
    stage: str
    backend: str
    prompt: str
    cwd: Path
    agent_spec: Optional[str] = None
    resume_session_id: Optional[str] = None
    timeout: Optional[int] = None
    extra_args: tuple[str, ...] = ()
    review_round: Optional[int] = None
    retry_count: Optional[int] = None


def _agent_run_kwargs(request: _AgentRunRequest) -> dict[str, Any]:
    """Forward only optional runner kwargs that the caller supplied."""
    kwargs: dict[str, Any] = {"extra_args": request.extra_args}
    if request.resume_session_id is not None:
        kwargs["resume_session_id"] = request.resume_session_id
    if request.timeout is not None:
        kwargs["timeout"] = request.timeout
    return kwargs


def _record_tracked_agent_exit(
    gh: GitHubClient,
    issue_number: int,
    request: _AgentRunRequest,
    agent_result: AgentResult,
    duration_s: float,
):
    gh.emit_event(
        "agent_exit",
        issue_number=issue_number,
        stage=request.stage,
        agent=request.backend,
        agent_role=request.agent_role,
        session_id=agent_result.session_id,
        duration_s=duration_s,
        exit_code=agent_result.exit_code,
        timed_out=agent_result.timed_out,
        review_round=request.review_round,
        retry_count=request.retry_count,
    )
    return analytics.record_agent_exit(
        repo=getattr(gh, "_repo_slug", None) or "",
        issue=issue_number,
        stage=request.stage,
        agent_role=request.agent_role,
        backend=request.backend,
        agent_spec=request.agent_spec,
        resume_session_id=request.resume_session_id,
        result=agent_result,
        duration_s=duration_s,
        review_round=request.review_round,
        retry_count=request.retry_count,
        fallback_model=_configured_model(request.backend, request.extra_args),
        prompt=request.prompt,
        cwd=request.cwd,
    )


def _emit_triggered_skills(
    gh: GitHubClient,
    issue_number: int,
    request: _AgentRunRequest,
    triggered_skills,
) -> None:
    try:
        for skill in triggered_skills or ():
            gh.emit_event(
                "skill_triggered",
                issue_number=issue_number,
                stage=request.stage,
                agent=request.backend,
                agent_role=request.agent_role,
                review_round=request.review_round,
                retry_count=request.retry_count,
                skill=skill,
            )
    except Exception:
        log.exception(
            "issue=#%d: skill_triggered audit emission failed; continuing",
            issue_number,
        )


def _run_agent_tracked(
    gh: GitHubClient,
    issue_number: int,
    request: Optional[_AgentRunRequest] = None,
    **request_fields: Any,
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
    stored in this `agent_exit` record -- the analytics sink is a foundation
    for usage / cost aggregation, not a debugging mirror, and `result.stdout`
    may contain user-issue text. A parser failure or a sink IO error is
    swallowed so an analytics misconfiguration cannot stop the per-issue tick.

    The returned `AgentResult` additionally carries the parsed run usage on its
    `usage` field -- `record_agent_exit` attaches the `UsageMetrics` it parsed
    from the same stdout, independent of whether the sink is enabled -- so
    callers can read token / cost metrics off the result without re-parsing.
    It is `None` when the usage parse failed (fail-open); this is best-effort
    observability plumbing and does not touch the pinned state.

    The `prompt` is forwarded to `record_agent_exit` so it can land as the
    redacted `user_input` of the separate, opt-in trajectory record -- and
    ONLY when `TRAJECTORY_LOG_PATH` is enabled. With the trajectory sink off
    (the default) the prompt is never stored and the `agent_exit` record
    shape is unchanged. That trajectory parse / redact / write rides its own
    fail-open guard inside `record_agent_exit`, so it never disturbs the
    baseline record or the `skill_triggered` events below.

    The worktree `cwd` is also forwarded so `record_agent_exit` can discover
    a codex run's offered skills out-of-band from the filesystem -- codex's
    stream carries no offered-skills catalog the way claude's `system`/`init`
    frame does, so this backfills `skills_available` for codex records.

    When `TRACK_SKILL_TRIGGERS` is on, `record_agent_exit` returns the
    distinct skills the run triggered and one `skill_triggered` audit event
    is emitted per skill (carrying `agent`, `agent_role`, `review_round`,
    `retry_count`, and `skill`), reusing that parsed list rather than
    re-reading stdout. The switch off (the default) yields no list and thus
    no events, so the gating is inherited from the analytics layer; the
    emission is wrapped in its own fail-open guard so an opt-in bug can never
    cost a run whose baseline `agent_spawn` / `agent_exit` events already
    fired.
    """
    if request is not None and request_fields:
        raise TypeError("pass either request or keyword request fields, not both")
    run_request = request or _AgentRunRequest(**request_fields)
    start = time.monotonic()
    gh.emit_event(
        "agent_spawn",
        issue_number=issue_number,
        stage=run_request.stage,
        agent=run_request.backend,
        agent_role=run_request.agent_role,
        session_id=run_request.resume_session_id,
        review_round=run_request.review_round,
        retry_count=run_request.retry_count,
    )
    # Forward only the kwargs the original call sites set so the
    # wrapper's run_agent invocation matches the pre-tracking signature
    # call-for-call (test fakes assert on `call.kwargs`).
    agent_result = run_agent(
        run_request.backend,
        run_request.prompt,
        run_request.cwd,
        **_agent_run_kwargs(run_request),
    )
    duration_s = round(time.monotonic() - start, 3)
    triggered_skills = _record_tracked_agent_exit(
        gh, issue_number, run_request, agent_result, duration_s,
    )
    # One `skill_triggered` audit event per distinct triggered skill, reusing
    # the list `record_agent_exit` already parsed (no second pass over stdout).
    # Empty unless `TRACK_SKILL_TRIGGERS` is on, so the gating is inherited
    # from the analytics layer. This is opt-in observability, so it rides its
    # own fail-open guard exactly like the skill parse does -- a bug here must
    # never break a run whose baseline audit events have already fired.
    _emit_triggered_skills(gh, issue_number, run_request, triggered_skills)
    return agent_result


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
    eq_prefix = f"{flag}="
    for arg_index, arg in enumerate(extra_args):
        if arg == flag and arg_index + 1 < len(extra_args):
            model_name = extra_args[arg_index + 1].strip()
            return model_name or None
        if arg.startswith(eq_prefix):
            model_name = arg[len(eq_prefix):].strip()
            return model_name or None
    return None


def _accumulate_issue_usage(
    state: PinnedState, usage: Optional[UsageMetrics]
) -> None:
    """Fold one agent run's parsed usage into the per-issue running totals.

    Called by the developer (implementing) and reviewer (validating) run
    sites right after `_run_agent_tracked` returns, mutating the SAME
    `PinnedState` the handler persists later -- never a second writer. The
    runner deliberately does not write pinned state itself, so an
    `interrupted` run whose handler returns without `write_pinned_state`
    (the shutdown-sweep contract) simply never persists these counters: a
    slight, accepted undercount on killed runs, with the analytics sink
    still holding ground truth.

    Keys folded (all new to the pinned-state schema):
      * ``issue_agent_runs``     -- +1 per real agent exit.
      * ``issue_total_tokens``   -- input + output + cache-read + cache-write.
        codex's ``cached_tokens`` is intentionally excluded: it is the
        portion of ``input_tokens`` already served from cache, so summing it
        would double-count part of the input.
      * ``issue_total_cost_usd`` -- sum of each run's ``cost_usd``; ``None``
        costs (``no-usage`` / ``unknown-price``) contribute nothing.
      * ``issue_cost_sources``   -- sorted distinct ``cost_source`` tags seen.
        The minimal aggregate a terminal verdict needs to mark ``(est.)``
        (any ``estimated``) or an unpriced ``unknown`` (any ``unknown-price``)
        without re-reading the analytics sink.

    A ``None`` usage -- the fail-open case where the parse itself failed --
    is a no-op: with no parsed metrics there is nothing to fold and the run
    is not counted.
    """
    if usage is None:
        return

    agent_runs = int(state.get("issue_agent_runs") or 0)
    state.set("issue_agent_runs", agent_runs + 1)

    tokens = sum((
        usage.input_tokens,
        usage.output_tokens,
        usage.cache_read_tokens,
        usage.cache_write_tokens,
    ))
    state.set(
        "issue_total_tokens",
        int(state.get("issue_total_tokens") or 0) + tokens,
    )

    if usage.cost_usd is not None:
        state.set(
            "issue_total_cost_usd",
            float(state.get("issue_total_cost_usd") or 0) + usage.cost_usd,
        )

    prior_sources = state.get("issue_cost_sources")
    seen = set(prior_sources) if isinstance(prior_sources, list) else set()
    seen.add(usage.cost_source)
    state.set("issue_cost_sources", sorted(seen))


def _format_issue_usage_verdict(state: PinnedState) -> Optional[str]:
    """Render the cumulative per-issue usage verdict for a terminal surface.

    Reads the counters `_accumulate_issue_usage` folds onto pinned state and
    returns a single visible line:

        :receipt: this issue: 3 agent runs · 45,200 tokens · $0.87

    The cost slot follows `issue_cost_sources`: `(est.)` is appended when any
    run's cost was `estimated` from the price table, and the whole figure
    collapses to `unknown` when any `unknown-price` run leaves the priced
    total incomplete (that dominates -- an unknown total cannot also be an
    estimate). A `no-usage` run contributes nothing and marks neither.

    Returns None when no agent run was ever counted (`issue_agent_runs` is
    0 / absent) so a terminal with nothing to report skips the line instead
    of posting a zero receipt.
    """
    runs = int(state.get("issue_agent_runs") or 0)
    if runs <= 0:
        return None
    tokens = int(state.get("issue_total_tokens") or 0)
    prior_sources = state.get("issue_cost_sources")
    sources = set(prior_sources) if isinstance(prior_sources, list) else set()
    if "unknown-price" in sources:
        cost = "unknown"
    else:
        cost = f"${float(state.get('issue_total_cost_usd') or 0):.2f}"
        if "estimated" in sources:
            cost = f"{cost} (est.)"
    return (
        f":receipt: this issue: {runs} agent runs · "
        f"{tokens:,} tokens · {cost}"
    )


def _post_issue_usage_verdict(
    gh: GitHubClient, issue: Issue, state: PinnedState
) -> None:
    """Post the terminal usage verdict as its own tracked issue comment.

    Thin wrapper over `_format_issue_usage_verdict` + `_post_issue_comment`
    for the PR merged / rejected finalizers, which otherwise post no comment
    of their own. Must run BEFORE the finalizer's `write_pinned_state` so the
    comment id lands in the same persisted state and a later drift/watermark
    tick recognizes it as orchestrator-authored. A no-op when there is
    nothing to report (no counted agent run).
    """
    verdict = _format_issue_usage_verdict(state)
    if verdict:
        _post_issue_comment(gh, issue, state, verdict)


@dataclass(frozen=True)
class _CommunityContribution:
    author: str


def _community_contribution_for_pr(
    gh: GitHubClient, pr, allowed_lower: set[str],
) -> Optional[_CommunityContribution]:
    user = getattr(pr, "user", None)
    if getattr(user, "type", None) == "Bot":
        return None
    author = getattr(user, "login", None) or ""
    if author.lower() in allowed_lower:
        return None
    if gh.pr_has_label(pr, COMMUNITY_CONTRIBUTION_LABEL):
        return None
    return _CommunityContribution(author)


def _label_community_contribution(
    gh: GitHubClient,
    spec: config.RepoSpec,
    pr,
    contribution: _CommunityContribution,
) -> None:
    # The label is the dedup marker, so the ping must land first. A label
    # failure may repeat a ping; a comment failure must not suppress one.
    author = contribution.author or "unknown"
    gh.pr_comment(
        pr.number,
        f"{config.HITL_MENTIONS} community contribution from "
        f"@{author} -- please review this PR.",
    )
    gh.add_pr_label(pr, COMMUNITY_CONTRIBUTION_LABEL)
    log.info(
        "repo=%s pr=#%s author=%r pinged HITL and labeled %r",
        spec.slug, pr.number, contribution.author, COMMUNITY_CONTRIBUTION_LABEL,
    )


def _sweep_pr_contribution(
    gh: GitHubClient, spec: config.RepoSpec, pr, allowed_lower: set,
) -> None:
    """Label one open PR when its author is an outside community contributor."""
    contribution = _community_contribution_for_pr(gh, pr, allowed_lower)
    if contribution is not None:
        _label_community_contribution(gh, spec, pr, contribution)


def _sweep_community_contribution_prs(
    gh: GitHubClient, spec: config.RepoSpec
) -> None:
    """Label open PRs from authors outside ALLOWED_ISSUE_AUTHORS and ping HITL.

    No-op when ALLOWED_ISSUE_AUTHORS is empty (the default) so a single-user
    deployment keeps the legacy "anyone is trusted" behavior. When the list
    is populated, every open PR whose author is not in it earns the
    `community_contribution` label and a one-shot HITL ping comment; the
    label is idempotent (already-labeled PRs are skipped) so the comment
    fires exactly once per PR.

    Bot-authored PRs (Dependabot, Renovate, CI bots) are skipped by
    GitHub's `user.type == "Bot"` flag -- they open PRs structurally and
    are not community contributions, so they never earn the label or ping.

    All errors are caught and logged: a PyGithub lazy-load failure on one
    PR must not abort the rest of the sweep, and the sweep itself must not
    abort the polling tick.
    """
    allowed = config.ALLOWED_ISSUE_AUTHORS
    if not allowed:
        return
    allowed_lower = {github_handle.lower() for github_handle in allowed}
    try:
        prs = list(gh.iter_open_prs())
    except Exception:
        log.exception(
            "repo=%s community-contribution sweep: open-PR enumeration failed",
            spec.slug,
        )
        return
    for pr in prs:
        try:
            _sweep_pr_contribution(gh, spec, pr, allowed_lower)
        except Exception:
            log.exception(
                "repo=%s pr=#%s community-contribution sweep step failed; continuing",
                spec.slug, getattr(pr, "number", "?"),
            )


@dataclass(frozen=True)
class _PollablePartition:
    """Family / fanout split of one repo's pollable issues for a single tick.

    ``family_numbers`` and ``family_labels`` are index-aligned so the
    cap-exempt decision (`_family_bucket_cap_exempt`) can read each
    family-aware issue's workflow label. ``fanout_closed`` is the subset of
    ``fanout_numbers`` whose issue is already closed -- a cheap terminal
    finalize the dispatcher submits cap-exempt.
    """
    family_numbers: list[int]
    family_labels: list[Optional[str]]
    fanout_numbers: list[int]
    fanout_closed: set[int]


@dataclass
class _PollablePartitionBuilder:
    family_numbers: list[int] = field(default_factory=list)
    family_labels: list[Optional[str]] = field(default_factory=list)
    fanout_numbers: list[int] = field(default_factory=list)
    fanout_closed: set[int] = field(default_factory=set)

    def add(self, issue_number: int, label: Optional[str], closed: bool) -> None:
        if label is None or label in _FAMILY_AWARE_LABELS:
            self.family_numbers.append(issue_number)
            self.family_labels.append(label)
        else:
            self.fanout_numbers.append(issue_number)
            if closed:
                self.fanout_closed.add(issue_number)

    def build(self) -> _PollablePartition:
        return _PollablePartition(
            self.family_numbers,
            self.family_labels,
            self.fanout_numbers,
            self.fanout_closed,
        )


def _read_issue_routing(
    gh: GitHubClient, spec: config.RepoSpec, issue: Issue,
) -> tuple[bool, Optional[str]]:
    """Return ``(skip, label)`` from the issue's control / workflow labels."""
    skip_label = hard_skip_control_label(issue)
    if skip_label is not None:
        log.info(
            "repo=%s issue=#%s has %r; skipping",
            spec.slug, issue.number, skip_label,
        )
        return True, None
    return False, gh.workflow_label(issue)


def _classify_pollable_issue(
    gh: GitHubClient, spec: config.RepoSpec, issue: Issue,
) -> tuple[bool, Optional[str]]:
    """Read one pollable issue's workflow label for the family / fanout split.

    Returns ``(skip, label)``. ``skip=True`` marks a hard-skip control label
    (``backlog`` / ``paused``): the operator parked the issue outside the
    state machine, so the caller drops it BEFORE the partition -- a parked,
    workflow-label-less issue folded into the family bucket would flip the
    whole bucket cap-counted and starve fanout under ``parallel_limit=1``
    (``_process_issue`` skips it anyway).

    A label-read failure (including one raised by ``hard_skip_control_label``
    itself) is reported as ``(False, None)`` so the issue is conservatively
    routed into the family bucket, where ``_process_issue``'s own per-issue
    exception isolation picks up any sustained failure. The label read runs
    on the caller thread so bucketing needs no extra worker-side round-trip.
    """
    try:
        return _read_issue_routing(gh, spec, issue)
    except Exception:
        log.exception(
            "repo=%s issue=#%s label read failed; routing to family bucket "
            "so per-issue exception isolation can pick up any sustained "
            "failure", spec.slug, issue.number,
        )
        return False, None


def _partition_pollable_issues(
    gh: GitHubClient, spec: config.RepoSpec,
) -> _PollablePartition:
    """Split this tick's pollable issues into the family and fanout buckets.

    Family-aware labels (``decomposing`` / ``blocked`` / ``umbrella``) and
    the unlabeled-pickup ``None`` are cross-issue writers -- a parent's
    ``_handle_decomposing`` recovery seeds ``parent_number`` on a child
    while the child's ``_handle_blocked`` would otherwise clobber the same
    pinned-state comment -- so they must never run two at a time and are
    collected into ``family_numbers`` (with index-aligned ``family_labels``).
    Every other label touches only its own per-issue state and fans out; a
    closed fanout issue is additionally recorded in ``fanout_closed`` because
    its handler is a cheap terminal finalize submitted cap-exempt. Hard-skip
    (``backlog`` / ``paused``) issues are dropped entirely.
    """
    builder = _PollablePartitionBuilder()
    for issue in gh.list_pollable_issues():
        skip, label = _classify_pollable_issue(gh, spec, issue)
        if skip:
            continue
        builder.add(int(issue.number), label, _issue_is_closed(issue))
    return builder.build()


def _family_bucket_cap_exempt(family_labels: list[Optional[str]]) -> bool:
    """True when a family bucket may skip the per-repo / global caps.

    A bucket is cap-exempt only when EVERY issue in it this tick runs a
    no-agent / no-worktree handler -- all labels in ``_CAP_EXEMPT_FAMILY_LABELS``
    (``blocked`` / ``umbrella``, pure dep-graph walks). Such a bucket must
    always get its turn even when the parallel caps are saturated by real
    implementation work: a ``blocked`` parent polling its children, or an
    ``umbrella`` aggregating them, would otherwise be starved of the only
    per-repo slot under the default ``parallel_limit=1`` -- and a ``blocked``
    parent waiting on its own children would deadlock them. A bucket
    containing ``decomposing`` (spawns the decomposer agent) or an
    unlabeled-pickup ``None`` (routes through ``_handle_pickup``, may spawn an
    agent) stays cap-counted.
    """
    return all(lbl in _CAP_EXEMPT_FAMILY_LABELS for lbl in family_labels)


def _refetch_and_process(
    gh: GitHubClient,
    spec: config.RepoSpec,
    issue_number: int,
    *,
    semaphore_cm: Optional[contextlib.AbstractContextManager] = None,
) -> None:
    """Mint a per-worker client, refetch the Issue, and run its handler.

    Only issue NUMBERS cross the thread boundary. PyGithub's ``Issue`` and
    the parent ``GitHubClient`` / ``Repository`` / ``Requester`` chain hold
    mutable per-request state that is not documented thread-safe, so each
    worker calls ``gh._for_worker_thread()`` to mint a fresh client and
    refetches its Issue against THAT client -- every in-flight HTTP call is
    then the sole consumer of its requester's state.

    ``semaphore_cm`` wraps the ``_process_issue`` call so the legacy parallel
    path can thread the cross-repo ``global_semaphore`` through here; the
    scheduler path leaves it ``None`` (a no-op) because the scheduler owns
    the cross-repo cap itself.
    """
    worker_gh = gh._for_worker_thread()
    worker_issue = worker_gh.get_issue(issue_number)
    cm = contextlib.nullcontext() if semaphore_cm is None else semaphore_cm
    with cm:
        _process_issue(worker_gh, spec, worker_issue)


def _run_sequential_tick(
    gh: GitHubClient,
    spec: config.RepoSpec,
    semaphore_cm: contextlib.AbstractContextManager,
) -> None:
    """Process this tick's pollable issues one at a time on the caller thread.

    `parallel_limit == 1` (the legacy default) streams directly over
    `gh.list_pollable_issues()` rather than materializing the list first.
    Materializing would change observable behavior on a partial enumeration
    failure (e.g. a PyGithub pagination error mid-sweep): the sequential loop
    processes everything yielded BEFORE the failure, but a `list(...)` upfront
    would lose every already-yielded issue when the generator raises. Each
    `_process_issue` is wrapped in its own try/except so one raising issue
    cannot stop the rest.
    """
    for issue in gh.list_pollable_issues():
        try:
            with semaphore_cm:
                _process_issue(gh, spec, issue)
        except Exception:
            log.exception(
                _PROCESSING_FAILED_LOG,
                spec.slug, issue.number,
            )


def _drain_family_bucket(
    gh: GitHubClient,
    spec: config.RepoSpec,
    family_numbers: list[int],
    *,
    semaphore_cm: contextlib.AbstractContextManager,
) -> None:
    """Process this tick's family-aware issues sequentially on one thread.

    The parallel path submits the whole family bucket as ONE executor task so
    its footprint stays at exactly one worker slot regardless of how many
    family-aware issues are pending, leaving the other `limit - 1` slots free
    for fanout. Per-issue exception isolation lives INSIDE this loop (one
    try/except per issue) so the bucket keeps draining if any single family
    handler raises; the function itself never raises, so the caller's
    `fut.result()` only ever surfaces a programming-level failure.
    """
    for issue_number in family_numbers:
        try:
            _refetch_and_process(
                gh, spec, issue_number, semaphore_cm=semaphore_cm,
            )
        except Exception:
            log.exception(
                _PROCESSING_FAILED_LOG,
                spec.slug, issue_number,
            )


@dataclass(frozen=True)
class _ParallelTickPlan:
    gh: GitHubClient
    spec: config.RepoSpec
    partition: _PollablePartition
    semaphore_cm: contextlib.AbstractContextManager

    @property
    def task_count(self) -> int:
        family_count = 1 if self.partition.family_numbers else 0
        return family_count + len(self.partition.fanout_numbers)

    def submit(self, executor) -> tuple[dict[Any, Any], object]:
        family_sentinel: object = object()
        futures: dict[Any, Any] = {}
        if self.partition.family_numbers:
            futures[
                executor.submit(
                    _drain_family_bucket,
                    self.gh,
                    self.spec,
                    self.partition.family_numbers,
                    semaphore_cm=self.semaphore_cm,
                )
            ] = family_sentinel
        for issue_number in self.partition.fanout_numbers:
            futures[
                executor.submit(
                    _refetch_and_process,
                    self.gh,
                    self.spec,
                    issue_number,
                    semaphore_cm=self.semaphore_cm,
                )
            ] = issue_number
        return futures, family_sentinel


def _drain_parallel_futures(
    spec: config.RepoSpec,
    futures: dict[Any, Any],
    family_sentinel: object,
) -> None:
    for future in as_completed(futures):
        tag = futures[future]
        try:
            future.result()
        except Exception:
            if tag is family_sentinel:
                # Per-issue failures are caught by the family drain itself;
                # only a programming-level drain failure reaches this path.
                log.exception(
                    "repo=%s family bucket drain raised (programming "
                    "error -- per-issue exceptions are handled inside "
                    "the drain)", spec.slug,
                )
            else:
                log.exception(
                    _PROCESSING_FAILED_LOG, spec.slug, tag,
                )


def _run_parallel_tick(
    gh: GitHubClient,
    spec: config.RepoSpec,
    limit: int,
    semaphore_cm: contextlib.AbstractContextManager,
) -> None:
    """Fan this tick's pollable issues out across a bounded thread pool.

    Family-aware (cross-issue writer) work is partitioned off from fanout so
    the family bucket drains sequentially inside ONE task while the rest fan
    out; `_partition_pollable_issues` owns the skip-label filtering, per-issue
    label-read isolation, and the family/fanout split. Each `_process_issue`
    is independent (per-issue worktree, PinnedState, GitHub label/comment
    surface) so worker threads serialize only at the PyGithub HTTP layer,
    which is already thread-safe.

    The executor needs the full submission set up front to bound
    `max_workers`, so the generator is materialized in `_partition_pollable_issues`;
    on an enumeration failure the whole tick aborts and the next tick's
    enumeration retries. Folding the whole family bucket into one drain task
    caps its footprint at exactly one executor slot regardless of how many
    family-aware issues there are, leaving the other `limit - 1` slots free
    for fanout -- submitting per-family-issue futures with a shared lock would
    instead let a waiting family future occupy the other worker slot and
    starve fanout under a small `limit`.
    """
    plan = _ParallelTickPlan(
        gh, spec, _partition_pollable_issues(gh, spec), semaphore_cm,
    )
    if plan.task_count == 0:
        return
    slug_token = spec.slug.replace("/", "__")
    # max_workers is capped at `limit` AND at the submitted-task count so a
    # quiet tick (e.g. one fan-out issue) does not spin up idle worker threads.
    with ThreadPoolExecutor(
        max_workers=min(limit, plan.task_count),
        thread_name_prefix=f"orch-{slug_token}",
    ) as executor:
        futures, family_sentinel = plan.submit(executor)
        # `as_completed` so a slow issue does not delay logging the failures
        # of faster ones. Each `fut.result()` is wrapped individually so one
        # raising issue cannot abort the remaining futures' result drain.
        _drain_parallel_futures(spec, futures, family_sentinel)


def tick(
    gh: GitHubClient,
    spec: config.RepoSpec,
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
    # Per-tick: label any open PR from an outsider author and ping HITL once.
    # Independent from the per-issue dispatch (PRs not driven by the
    # orchestrator have no pinned state to consult), so failures inside the
    # sweep are swallowed by the helper itself and cannot stop the tick.
    _sweep_community_contribution_prs(gh, spec)
    # Per-tick: snapshot the target repo's skill catalog into analytics.
    # Runs after the base refresh above has fetched
    # `<remote_name>/<base_branch>` so the ls-tree reads the current base
    # ref. Producer-side observability only and internally fail-open, so a
    # missing clone / git error never stops the tick; placed before the
    # scheduler/legacy split so it fires once per tick on both paths.
    _emit_repo_skill_catalog(spec)
    if scheduler is not None:
        _dispatch_via_scheduler(gh, spec, scheduler)
        return
    # `parallel_limit` is the local cap on worker threads this tick spins up.
    # The host-wide `MAX_PARALLEL_ISSUES_GLOBAL` cap is enforced by
    # `global_semaphore` around each `_process_issue` call, not by shrinking
    # the worker pool: with multiple repos ticking in parallel, workers from
    # different repos may queue on the semaphore until a global slot frees up,
    # which is the whole point of a cross-repo cap. None falls back to a no-op
    # context manager so a direct test invocation of `tick(gh, spec)` keeps
    # working unchanged. `limit == 1` (the legacy default) stays sequential
    # and in-thread; `limit > 1` fans out across a bounded pool.
    limit = max(1, int(getattr(spec, "parallel_limit", 1) or 1))
    semaphore_cm = (
        contextlib.nullcontext() if global_semaphore is None else global_semaphore
    )
    if limit == 1:
        _run_sequential_tick(gh, spec, semaphore_cm)
    else:
        _run_parallel_tick(gh, spec, limit, semaphore_cm)


_FAMILY_BUCKET_ISSUE: int = 0
"""Sentinel ``issue_number`` for the family-bucket submit.

The scheduler's duplicate-active gate keys on ``(repo_slug, issue_number)``.
The family bucket is one submit per repo that drains every family-aware
issue sequentially, so it needs a key that cannot collide with a real
issue. GitHub issue numbers are strictly positive, so 0 is safe.
"""


def _issue_is_closed(issue) -> bool:
    """True when the issue is closed.

    Tolerant of both shapes the dispatcher sees: PyGithub's ``Issue.state``
    (``"open"`` / ``"closed"``) and the in-memory fake's ``closed`` bool.
    """
    return bool(getattr(issue, "closed", False)) or (
        getattr(issue, _STATE_ATTR, _ISSUE_STATE_OPEN) == _ISSUE_STATE_CLOSED
    )


def _drain_scheduler_family_bucket(
    gh: GitHubClient,
    spec: config.RepoSpec,
    scheduler: IssueScheduler,
    family_numbers: list[int],
) -> None:
    """Drain this tick's family-aware issues sequentially under one bucket.

    Runs as the single ``family=True`` scheduler submit per repo, so the
    family slot is held for the whole drain: a concurrent tick mid-drain
    cannot squeeze a second family worker past the gate and no two
    family-aware handlers ever run at once. ``scheduler.track_active`` wraps
    each iteration so ``is_active(repo, n)`` reports True for the issue
    currently being processed inside the bucket -- the pre-tick base refresh
    relies on that signal to avoid rebasing a worktree under a running agent;
    without the per-iteration claim only the bucket's sentinel key would
    appear in the in-flight set and a concurrent refresh would race the agent.

    ``track_active`` yields a ``claimed`` bool: when False the issue is
    already in flight on another worker (e.g. a fanout submit accepted on a
    previous tick before this issue was relabeled into the family bucket), so
    the drain skips ``_process_issue`` for that iteration and the next polling
    pass picks it up once the other worker exits -- two workers running the
    same handler concurrently would race the worktree and pinned state.
    Per-issue exception isolation lives inside the loop so one raising family
    handler does not abort the rest of the bucket.

    Each per-issue call mirrors the fanout path: ``_refetch_and_process``
    mints a fresh ``GitHubClient`` via ``gh._for_worker_thread()`` and
    refetches the Issue against it (PyGithub is not documented thread-safe).
    """
    for issue_number in family_numbers:
        try:
            with scheduler.track_active(spec.slug, issue_number) as claimed:
                if not claimed:
                    log.info(
                        "repo=%s issue=#%s already in flight; "
                        "family bucket skipping this iteration",
                        spec.slug, issue_number,
                    )
                    continue
                _refetch_and_process(gh, spec, issue_number)
        except Exception:
            log.exception(
                _PROCESSING_FAILED_LOG,
                spec.slug, issue_number,
            )


def _scheduler_per_repo_cap(spec: config.RepoSpec) -> int:
    return max(1, int(getattr(spec, "parallel_limit", 1) or 1))


def _submit_scheduler_family_bucket(
    gh: GitHubClient,
    spec: config.RepoSpec,
    scheduler: IssueScheduler,
    partition: _PollablePartition,
    per_repo_cap: int,
) -> None:
    family_numbers = partition.family_numbers
    if not family_numbers:
        return

    submitted = scheduler.submit(
        spec.slug,
        _FAMILY_BUCKET_ISSUE,
        functools.partial(
            _drain_scheduler_family_bucket, gh, spec, scheduler, family_numbers,
        ),
        family=True,
        cap_exempt=_family_bucket_cap_exempt(partition.family_labels),
        per_repo_cap=per_repo_cap,
    )
    if submitted:
        return

    # The scheduler logs the precise skip reason (closed, family_slot_held,
    # cap, ...) inside `submit`; this line gives the dispatch-layer context
    # -- which issues were waiting on this bucket -- so an operator can
    # correlate "umbrella not advancing" with a previous tick's bucket
    # still in flight.
    log.info(
        "repo=%s family bucket (%d issues) not submitted this "
        "tick; next polling pass retries",
        spec.slug, len(family_numbers),
    )


def _submit_scheduler_fanout_issues(
    gh: GitHubClient,
    spec: config.RepoSpec,
    scheduler: IssueScheduler,
    partition: _PollablePartition,
    per_repo_cap: int,
) -> None:
    for issue_number in partition.fanout_numbers:
        scheduler.submit(
            spec.slug,
            issue_number,
            functools.partial(_refetch_and_process, gh, spec, issue_number),
            family=False,
            # A closed issue's handler is a cheap terminal finalization with
            # no agent spawn -- exempt it from the per-repo / global caps so
            # a merged-PR or closed-question issue flips to `done` promptly
            # instead of being starved behind active agent work under
            # `parallel_limit=1` (mirrors the `_CAP_EXEMPT_FAMILY_LABELS`
            # exemption for `blocked` / `umbrella`).
            cap_exempt=(issue_number in partition.fanout_closed),
            per_repo_cap=per_repo_cap,
        )


def _dispatch_via_scheduler(
    gh: GitHubClient, spec: config.RepoSpec, scheduler: IssueScheduler,
) -> None:
    """Enumerate pollable issues this tick and hand work to the scheduler.

    Family-aware work (unlabeled pickup + decomposing / blocked /
    umbrella -- the cross-issue writers) is folded into ONE bucket
    submit per repo that drains its issues sequentially on a single
    worker thread; non-family issues are submitted individually. This
    mirrors the legacy parallel-tick partition in ``tick()`` (one drain
    task for the family bucket, per-issue futures for fanout).

    Per-submitting family-aware issues with `family=True` (the prior
    behavior) lets the first accepted family submit hold the family
    slot and silently starve every subsequent family submit this tick.
    The starvation was the issue #326 bug: a stale backlog/blocked
    child took the slot and the parent umbrella that should have
    relabeled it never ran. Folding family work into one bucket means
    the umbrella always gets its turn within the same tick.

    The bucket task uses ``scheduler.track_active`` around each
    per-issue iteration so ``scheduler.is_active(repo, n)`` reports True
    for the issue currently being processed inside the bucket -- the
    pre-tick base refresh relies on that signal to avoid rebasing a
    worktree under a running agent. Without per-iteration tracking,
    only the bucket's sentinel key would appear in the in-flight set
    and a concurrent refresh would race the agent.

    Each per-issue callable mirrors the legacy parallel path: mint a
    fresh ``GitHubClient`` via ``gh._for_worker_thread()`` and refetch
    the Issue against that client so the worker drives its own
    Requester chain (PyGithub is not documented thread-safe).

    Completion reaping is the polling loop's job, not this function's.
    ``main._run_tick`` calls ``scheduler.reap()`` exactly once after
    every configured repo's tick returns, so the contract surfaced to
    operators and documented in ``docs/observability.md`` ("one reap
    per polling pass") holds in multi-repo mode too. An earlier draft
    reaped here as well; that produced N+1 reaps per polling pass
    under ``REPOS`` and contradicted the documented cadence.

    ``spec.parallel_limit`` is forwarded as the scheduler's per-call cap
    override so a per-repo configuration tighter than the scheduler
    default still binds. Label-read failures route the offending issue
    into the family bucket so ``_process_issue``'s own exception
    isolation picks up any sustained failure -- same recovery the
    legacy parallel path uses.

    When every family-aware issue this tick runs a no-agent handler
    (label in ``_CAP_EXEMPT_FAMILY_LABELS`` -- ``blocked`` or
    ``umbrella``, both pure label/dep-graph walks), the bucket submit is
    marked ``cap_exempt=True`` so it does not consume a
    ``MAX_PARALLEL_ISSUES_PER_REPO`` or ``MAX_PARALLEL_ISSUES_GLOBAL``
    slot. Such a bucket must always get its turn even when the caps are
    saturated by ordinary implementation work -- otherwise a ``blocked``
    parent polling its own children would be starved of the only
    per-repo slot (under the default ``parallel_limit=1``) and deadlock
    the very children it waits on. A bucket containing ``decomposing``
    (spawns the decomposer agent) or an unlabeled-pickup ``None`` stays
    cap-counted. ``backlog`` / ``paused`` issues are filtered out before
    this split -- a parked issue carries no workflow label, so leaving it in
    would fold it into the bucket and force ``cap_exempt=False``, starving
    fanout behind a hard-skip hold under ``parallel_limit=1``. The family mutex
    still applies, so a follow-up tick that finds another family issue
    still serializes against this bucket.

    Closed fan-out issues are likewise submitted ``cap_exempt=True``: a
    closed issue carrying a sweep label (``in_review`` / ``fixing`` /
    ``resolving_conflict`` / ``question`` / ...) only runs a terminal
    finalization (flip to ``done`` / ``rejected`` + branch cleanup) with no
    agent spawn, so it must not be starved behind active agent work -- a
    merged-PR issue could otherwise sit closed-but-labeled for many ticks
    while a sibling ``validating`` / ``documenting`` agent holds the only
    per-repo slot.
    """
    per_repo_cap = _scheduler_per_repo_cap(spec)
    # `_partition_pollable_issues` owns the skip-label filtering, per-issue
    # label-read isolation, and the family/fanout split (including the closed
    # fan-out set). `backlog` / `paused` issues are dropped there so a parked,
    # workflow-label-less issue never folds into the bucket and flips it
    # cap-counted, which would reserve the only per-repo slot and starve
    # fanout under `parallel_limit=1`.
    partition = _partition_pollable_issues(gh, spec)

    # One `family=True` submit per repo drains every family-aware issue
    # sequentially (see `_drain_scheduler_family_bucket`). The bucket is
    # cap-exempt only when every family issue runs a no-agent handler
    # (`_family_bucket_cap_exempt`); the helper keeps the exempt probe and
    # the submit off the no-family path entirely.
    _submit_scheduler_family_bucket(gh, spec, scheduler, partition, per_repo_cap)
    _submit_scheduler_fanout_issues(gh, spec, scheduler, partition, per_repo_cap)


_ISSUE_HANDLER_NAMES: Mapping[Optional[str], str] = MappingProxyType({
    None: "_handle_pickup",
    "decomposing": "_handle_decomposing",
    "ready": "_handle_ready",
    "blocked": "_handle_blocked",
    "umbrella": "_handle_umbrella",
    "implementing": "_handle_implementing",
    "documenting": "_handle_documenting",
    "validating": "_handle_validating",
    "in_review": "_handle_in_review",
    "fixing": "_handle_fixing",
    "resolving_conflict": "_handle_resolving_conflict",
    "question": "_handle_question",
})


def _route_issue_to_handler(
    gh: GitHubClient, spec: config.RepoSpec, issue: Issue, label: Optional[str],
) -> None:
    """Dispatch one issue to its stage handler by workflow label.

    The handlers are looked up as module globals so a test that patches
    ``workflow._handle_<stage>`` intercepts the call even though the dispatch
    lives here. ``done`` / ``rejected`` are terminal no-ops; an unrecognized
    label is logged and left alone for a human. Timing and the
    ``stage_evaluation`` analytics record stay in ``_process_issue``, which
    wraps this call in its try / except / finally.
    """
    handler_name = _ISSUE_HANDLER_NAMES.get(label)
    if handler_name is not None:
        issue_handler = getattr(sys.modules[__name__], handler_name)
        issue_handler(gh, spec, issue)
    elif label not in ("done", "rejected"):
        log.warning(
            "repo=%s issue=#%s label=%r not implemented yet; leaving alone",
            spec.slug, issue.number, label,
        )


def _process_issue(gh: GitHubClient, spec: config.RepoSpec, issue: Issue) -> None:
    # Postponed-task hold: applying `backlog` (or `paused`) parks the issue
    # outside the state machine entirely until the label is removed. Checked
    # before reading the workflow label so the orchestrator never decomposes,
    # spawns an agent, or otherwise reacts while the operator is using the
    # label as a "not yet" signal. Hard-skips are NOT counted as a stage
    # evaluation: no handler runs and there is nothing to time.
    skip_label = hard_skip_control_label(issue)
    if skip_label is not None:
        log.info(
            "repo=%s issue=#%s has %r; skipping",
            spec.slug, issue.number, skip_label,
        )
        return
    label = gh.workflow_label(issue)
    log.info("repo=%s issue=#%s label=%r", spec.slug, issue.number, label)
    # Time the handler dispatch and append a single `stage_evaluation`
    # analytics record on exit. `evaluation_result` flips to "error" inside the
    # except clause so an unhandled exception still produces a timing
    # record before propagating -- the tick loop's per-issue try/except
    # already logs and isolates the failure, so re-raising here keeps
    # the existing dispatch / exception contract intact. The append
    # itself is internally hardened against OSError; an analytics
    # misconfiguration cannot stop the per-issue tick from advancing.
    start = time.monotonic()
    evaluation_result = "ok"
    try:
        _route_issue_to_handler(gh, spec, issue, label)
    except Exception:
        evaluation_result = "error"
        raise
    finally:
        duration_s = round(time.monotonic() - start, 3)
        analytics.record_stage_evaluation(
            repo=getattr(gh, "_repo_slug", None) or "",
            issue=issue.number,
            stage=label,
            duration_s=duration_s,
            result=evaluation_result,
        )


def _pickup_author_allowed(spec: config.RepoSpec, issue: Issue) -> bool:
    # Author allowlist: when configured, silently skip unlabeled issues from
    # anyone outside the list so random users can't burn agent budget on a
    # public repo. Maintainers can still drive an outsider's issue manually
    # by adding a workflow label themselves -- the guard only fires here.
    if not config.ALLOWED_ISSUE_AUTHORS:
        return True
    author = getattr(getattr(issue, "user", None), "login", None) or ""
    allowed = {
        github_handle.lower()
        for github_handle in config.ALLOWED_ISSUE_AUTHORS
    }
    if author.lower() in allowed:
        return True
    log.info(
        "repo=%s issue=#%s author=%r not in ALLOWED_ISSUE_AUTHORS; skipping pickup",
        spec.slug, issue.number, author,
    )
    return False


def _record_pickup_comment(state: PinnedState, pickup) -> None:
    pickup_id = getattr(pickup, "id", None)
    if pickup_id is not None:
        state.set("pickup_comment_id", int(pickup_id))


def _start_decomposing(
    gh: GitHubClient, spec: config.RepoSpec, issue: Issue, state: PinnedState,
) -> None:
    pickup = _post_issue_comment(
        gh, issue, state,
        ":robot: orchestrator picking this up; decomposing.",
    )
    _record_pickup_comment(state, pickup)
    state.set(
        "user_content_hash",
        _compute_user_content_hash(issue, _orchestrator_ids(state)),
    )
    gh.set_workflow_label(issue, WorkflowLabel.DECOMPOSING)
    gh.write_pinned_state(issue, state)
    _handle_decomposing(gh, spec, issue)


def _start_implementing(
    gh: GitHubClient, spec: config.RepoSpec, issue: Issue, state: PinnedState,
) -> None:
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
    _record_pickup_comment(state, pickup)
    state.set(
        "user_content_hash",
        _compute_user_content_hash(issue, _orchestrator_ids(state)),
    )
    gh.set_workflow_label(issue, WorkflowLabel.IMPLEMENTING)
    gh.write_pinned_state(issue, state)
    _handle_implementing(gh, spec, issue)


def _handle_pickup(gh: GitHubClient, spec: config.RepoSpec, issue: Issue) -> None:
    if not _pickup_author_allowed(spec, issue):
        return
    state = PinnedState()
    state.set("created_at", _now_iso())
    if config.DECOMPOSE:
        _start_decomposing(gh, spec, issue, state)
    else:
        _start_implementing(gh, spec, issue, state)


def _ignore_if_interrupted(issue: Issue, agent_result: AgentResult) -> bool:
    """True when `agent_result` came from a run the shutdown sweep killed
    mid-flight (SIGTERM/SIGKILL -- `AgentResult.interrupted`).

    Such a run carries no trustworthy outcome: `last_message` is empty or a
    partial transcript chunk and no commit / question / timeout signal can be
    read from it. Dev-resume stage handlers call this BEFORE their
    timeout/question/dirty/push branches and `return` WITHOUT writing pinned
    state on a True result, so durable GitHub state stays exactly as the prior
    tick left it and the next orchestrator process re-runs the resume from
    scratch. Returning quietly here is what keeps the interrupted path from
    posting an agent-question HITL comment, consuming an `awaiting_human`
    park, advancing an action/comment watermark, or interpreting partial
    `last_message` content -- all of which the in-memory `state` mutations the
    caller already made would persist on a normal `write_pinned_state`.

    Logs once at INFO so the interruption is visible without being mistaken
    for a real silence/timeout park.
    """
    if not agent_result.interrupted:
        return False
    log.info(
        "issue=#%d agent run interrupted by shutdown sweep; leaving durable "
        "state untouched for retry by the next process",
        issue.number,
    )
    return True


def _paused_during_agent_run(gh: GitHubClient, issue: Issue) -> bool:
    """True when a hard-skip control label (`paused` / `backlog`) was applied
    to `issue` while an agent run was in flight.

    The dispatcher and `_process_issue` read the issue's labels once, at tick
    start, and skip a hard-skipped issue before any handler runs. But a stage
    that spawns an agent holds that label snapshot for the whole run -- minutes,
    typically -- so an operator who applies `paused` mid-run would otherwise not
    take effect until the run's results were already published: PR opened, label
    flipped, HITL park posted, action watermark consumed, pinned state advanced.

    Stage handlers call this right after an agent run returns, BEFORE any of
    that disposition, and `return` WITHOUT writing pinned state on a True result
    -- mirroring `_ignore_if_interrupted`. Durable GitHub state is left exactly
    as the prior tick had it and the agent's committed work stays on the branch,
    so once the operator removes the label the next tick republishes it through
    the normal recovered-worktree path.

    The label is read from a FRESHLY fetched issue (`gh.get_issue`), never the
    stale handler `issue` whose labels were snapshotted before the run -- the
    whole point is to catch a label applied mid-run. A fetch failure returns
    False (publish as before): the guard is an additive safety net and must not
    itself strand a run that would otherwise have completed.
    """
    try:
        fresh = gh.get_issue(issue.number)
    except Exception:
        log.debug(
            "issue=#%d not retrievable for post-agent pause check; proceeding",
            issue.number,
        )
        return False
    skip_label = hard_skip_control_label(fresh)
    if skip_label is None:
        return False
    log.info(
        "issue=#%d acquired %r during the agent run; leaving durable state "
        "untouched until the label is removed",
        issue.number, skip_label,
    )
    return True


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
    gh: GitHubClient, spec: config.RepoSpec, issue: Issue, state: PinnedState,
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
    _finalize_merged_pr(
        _ReviewTerminalContext(
            gh=gh,
            spec=spec,
            issue=issue,
            state=state,
            pr=pr,
            stage=gh.workflow_label(issue),
        ),
        close_error="could not close after detecting external merge",
        close_if_open_only=True,
    )
    return True


@dataclass(frozen=True)
class _ReviewTerminalContext:
    gh: GitHubClient
    spec: config.RepoSpec
    issue: Issue
    state: PinnedState
    pr: Any
    stage: Optional[str]

    @property
    def pr_number(self) -> int:
        return int(self.state.get("pr_number"))

    @property
    def conflict_round(self):
        conflict_round = self.state.get("conflict_round")
        if self.stage == "resolving_conflict":
            return int(conflict_round or 0)
        return conflict_round


def _close_terminal_issue(
    context: _ReviewTerminalContext, error_message: str,
) -> None:
    try:
        context.issue.edit(state=_ISSUE_STATE_CLOSED)
    except Exception:
        log.exception(
            "issue=#%s %s", context.issue.number, error_message,
        )


def _cleanup_review_terminal(context: _ReviewTerminalContext) -> None:
    _cleanup_terminal_branch(
        context.gh,
        context.spec,
        context.issue.number,
        branch=_resolve_branch_name(
            context.state, context.spec, context.issue.number,
        ),
    )


def _finalize_merged_pr(
    context: _ReviewTerminalContext,
    *,
    close_error: str,
    close_if_open_only: bool = False,
) -> None:
    context.state.set("merged_at", _now_iso())
    context.gh.set_workflow_label(context.issue, WorkflowLabel.DONE)
    _post_issue_usage_verdict(context.gh, context.issue, context.state)
    context.gh.write_pinned_state(context.issue, context.state)
    context.gh.emit_event(
        "pr_merged",
        issue_number=context.issue.number,
        stage=context.stage,
        pr_number=context.pr_number,
        sha=getattr(context.pr.head, "sha", None) or None,
        merge_method="external",
        review_round=int(context.state.get("review_round") or 0),
        conflict_round=context.conflict_round,
        retry_count=context.state.get("retry_count"),
    )
    if (
        not close_if_open_only
        or getattr(context.issue, _STATE_ATTR, _ISSUE_STATE_OPEN) != _ISSUE_STATE_CLOSED
    ):
        _close_terminal_issue(context, close_error)
    _cleanup_review_terminal(context)


def _finalize_rejected_pr(context: _ReviewTerminalContext) -> None:
    context.state.set("closed_without_merge_at", _now_iso())
    context.gh.set_workflow_label(context.issue, WorkflowLabel.REJECTED)
    _post_issue_usage_verdict(context.gh, context.issue, context.state)
    context.gh.write_pinned_state(context.issue, context.state)
    context.gh.emit_event(
        "pr_closed_without_merge",
        issue_number=context.issue.number,
        stage=context.stage,
        pr_number=context.pr_number,
        sha=getattr(context.pr.head, "sha", None) or None,
        review_round=int(context.state.get("review_round") or 0),
        conflict_round=context.conflict_round,
        retry_count=context.state.get("retry_count"),
    )
    _close_terminal_issue(context, "could not close after reject")
    _cleanup_review_terminal(context)


def _finalize_closed_issue_with_open_pr(context: _ReviewTerminalContext) -> None:
    context.state.set("closed_without_merge_at", _now_iso())
    context.gh.set_workflow_label(context.issue, WorkflowLabel.REJECTED)
    _post_issue_usage_verdict(context.gh, context.issue, context.state)
    context.gh.write_pinned_state(context.issue, context.state)


def _drain_review_terminal(context: _ReviewTerminalContext) -> bool:
    if context.pr is None:
        return False
    pr_status = context.gh.pr_state(context.pr)
    if pr_status == "merged":
        _finalize_merged_pr(context, close_error="could not close after merge")
        return True
    if pr_status == _ISSUE_STATE_CLOSED:
        _finalize_rejected_pr(context)
        return True
    if getattr(context.issue, _STATE_ATTR, _ISSUE_STATE_OPEN) == _ISSUE_STATE_CLOSED:
        _finalize_closed_issue_with_open_pr(context)
        return True
    return False


def _drain_review_pr_terminals(
    gh: GitHubClient,
    *context_args,
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
    spec, issue, state, pr = context_args
    return _drain_review_terminal(
        _ReviewTerminalContext(gh, spec, issue, state, pr, stage),
    )


@dataclass(frozen=True)
class _ClosedIssuePR:
    number: Optional[int]
    pr: Any = None
    defer: bool = False


def _closed_issue_pr(
    gh: GitHubClient, issue: Issue, state: PinnedState,
) -> _ClosedIssuePR:
    raw_number = state.get("pr_number")
    if raw_number is None:
        return _ClosedIssuePR(number=None)
    number = int(raw_number)
    try:
        pr = gh.get_pr(number)
    except Exception:
        log.exception(
            "issue=#%s could not fetch PR #%s while finalizing a "
            "closed issue; deferring (next tick retries the "
            "merged-PR path)", issue.number, raw_number,
        )
        return _ClosedIssuePR(number=number, defer=True)
    return _ClosedIssuePR(
        number=number,
        pr=pr,
        defer=gh.pr_state(pr) == "merged",
    )


def _emit_closed_pr_rejection(context: _ReviewTerminalContext) -> None:
    context.gh.emit_event(
        "pr_closed_without_merge",
        issue_number=context.issue.number,
        stage=context.stage,
        pr_number=context.pr_number,
        sha=getattr(context.pr.head, "sha", None) or None,
        review_round=int(context.state.get("review_round") or 0),
        conflict_round=context.state.get("conflict_round"),
        retry_count=context.state.get("retry_count"),
    )
    _cleanup_review_terminal(context)


def _finalize_if_issue_closed(
    gh: GitHubClient, spec: config.RepoSpec, issue: Issue, state: PinnedState,
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
    if getattr(issue, _STATE_ATTR, _ISSUE_STATE_OPEN) != _ISSUE_STATE_CLOSED:
        return False
    linked_pr = _closed_issue_pr(gh, issue, state)
    if linked_pr.defer:
        return True
    context = _ReviewTerminalContext(
        gh, spec, issue, state, linked_pr.pr, gh.workflow_label(issue),
    )
    _finalize_closed_issue_with_open_pr(context)
    if linked_pr.pr is not None and gh.pr_state(linked_pr.pr) == _ISSUE_STATE_CLOSED:
        _emit_closed_pr_rejection(context)
    return True
