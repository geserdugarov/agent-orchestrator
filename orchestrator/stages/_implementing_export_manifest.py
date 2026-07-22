# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Immutable lazy-export inventory for :mod:`orchestrator.stages.implementing`."""

from __future__ import annotations

from orchestrator._compat_exports import export_group

EXPORTS = (
    *export_group(
        "dataclasses",
        (("dataclass", "dataclass"),),
    ),
    *export_group(
        "github.Issue",
        (("Issue", "Issue"),),
    ),
    *export_group(
        "orchestrator._workflow_dependencies",
        (("config", "config"),),
    ),
    *export_group(
        "orchestrator.agents",
        (("AgentResult", "AgentResult"),),
    ),
    *export_group(
        "orchestrator.comment_trust",
        (("filter_trusted", "filter_trusted"),),
    ),
    *export_group(
        "orchestrator.github",
        (
            ("GitHubClient", "GitHubClient"),
            ("PinnedState", "PinnedState"),
        ),
    ),
    *export_group(
        "orchestrator.stages._implement_state",
        (
            ("_AGENT_TIMEOUT", "_AGENT_TIMEOUT"),
            ("_AWAITING_HUMAN", "_AWAITING_HUMAN"),
            ("_BRANCH", "_BRANCH"),
            ("_CLAUDE_CONTEXT_OVERFLOW_MARKERS", "_CLAUDE_CONTEXT_OVERFLOW_MARKERS"),
            ("_CLAUDE_SESSION_LIMIT_MESSAGE_MARKERS", "_CLAUDE_SESSION_LIMIT_MESSAGE_MARKERS"),
            ("_CLAUDE_STALE_SESSION_STDERR_MARKERS", "_CLAUDE_STALE_SESSION_STDERR_MARKERS"),
            ("_CODEX_SESSION_ID", "_CODEX_SESSION_ID"),
            ("_DEV_AGENT", "_DEV_AGENT"),
            ("_DEV_RESUME_COUNT", "_DEV_RESUME_COUNT"),
            ("_DEV_SESSION_ID", "_DEV_SESSION_ID"),
            ("_IMPLEMENTING_STAGE", "_IMPLEMENTING_STAGE"),
            ("_LAST_ACTION_COMMENT_ID", "_LAST_ACTION_COMMENT_ID"),
            ("_PARK_REASON", "_PARK_REASON"),
            ("_PRE_IMPLEMENT_SHA", "_PRE_IMPLEMENT_SHA"),
            ("_PR_BODY_AGENT_MESSAGE_CAP", "_PR_BODY_AGENT_MESSAGE_CAP"),
            ("_PR_BODY_TRUNCATION_MARKER", "_PR_BODY_TRUNCATION_MARKER"),
            ("_REASON_STUCK", "_REASON_STUCK"),
            ("_RETRY_COUNT", "_RETRY_COUNT"),
            ("_RETRY_WINDOW_START", "_RETRY_WINDOW_START"),
            ("_SILENT_PARKS_BEFORE_FRESH_SESSION", "_SILENT_PARKS_BEFORE_FRESH_SESSION"),
            ("_SILENT_PARK_COUNT", "_SILENT_PARK_COUNT"),
        ),
    ),
    *export_group(
        "orchestrator.stages._implementing_continue",
        (
            ("_ParkedContinueDecision", "_ParkedContinueDecision"),
            ("_handle_parked_continue_command", "_handle_parked_continue_command"),
            ("_parked_continue_decision", "_parked_continue_decision"),
            ("_retry_parked_dev_session", "_retry_parked_dev_session"),
        ),
    ),
    *export_group(
        "orchestrator.stages._implementing_drift",
        (
            ("_ImplementingDriftRun", "_ImplementingDriftRun"),
            ("_dispose_implementing_drift", "_dispose_implementing_drift"),
            ("_handle_user_content_drift", "_handle_user_content_drift"),
            ("_implementing_drift_run", "_implementing_drift_run"),
            ("_post_implementing_drift_ack", "_post_implementing_drift_ack"),
            ("_resume_dev_on_implementing_drift", "_resume_dev_on_implementing_drift"),
            ("_run_implementing_drift_resume", "_run_implementing_drift_resume"),
        ),
    ),
    *export_group(
        "orchestrator.stages._implementing_drift_preflight",
        (
            ("_handle_pre_session_drift", "_handle_pre_session_drift"),
            ("_prepare_awaiting_dev_run", "_prepare_awaiting_dev_run"),
            ("_recover_quiet_implementer_timeout", "_recover_quiet_implementer_timeout"),
        ),
    ),
    *export_group(
        "orchestrator.stages._implementing_handler",
        (
            ("_dispose_agent_result", "_dispose_agent_result"),
            ("_handle_detected_implementing_drift", "_handle_detected_implementing_drift"),
            ("_handle_implementing", "_handle_implementing"),
            ("_implementing_preflight", "_implementing_preflight"),
        ),
    ),
    *export_group(
        "orchestrator.stages._implementing_models",
        (
            ("_AgentWork", "_AgentWork"),
            ("_DevResumeOptions", "_DevResumeOptions"),
            ("_DevResumePlan", "_DevResumePlan"),
            ("_DevSession", "_DevSession"),
            ("_PRWork", "_PRWork"),
            ("_PreparedDevRun", "_PreparedDevRun"),
        ),
    ),
    *export_group(
        "orchestrator.stages._implementing_parks",
        (
            ("_dirty_worktree_message", "_dirty_worktree_message"),
            ("_mark_agent_silent_park", "_mark_agent_silent_park"),
            ("_on_dirty_worktree", "_on_dirty_worktree"),
            ("_on_question", "_on_question"),
            ("_park_real_question", "_park_real_question"),
            ("_park_session_limit", "_park_session_limit"),
            ("_park_silent_failure", "_park_silent_failure"),
        ),
    ),
    *export_group(
        "orchestrator.stages._implementing_publication",
        (
            ("_advance_to_validating", "_advance_to_validating"),
            ("_build_pr_body", "_build_pr_body"),
            ("_derive_pr_title", "_derive_pr_title"),
            ("_format_pr_agent_message", "_format_pr_agent_message"),
            ("_on_commits", "_on_commits"),
            ("_reset_implementing_counters", "_reset_implementing_counters"),
            ("_reuse_or_open_pr", "_reuse_or_open_pr"),
        ),
    ),
    *export_group(
        "orchestrator.stages._implementing_question_relabel",
        (
            ("_QuestionRelabelHazard", "_QuestionRelabelHazard"),
            ("_clear_stale_question_park", "_clear_stale_question_park"),
            ("_handle_stale_question_park", "_handle_stale_question_park"),
            ("_park_unsafe_question_relabel", "_park_unsafe_question_relabel"),
            ("_question_relabel_hazard", "_question_relabel_hazard"),
            ("_question_relabel_trigger", "_question_relabel_trigger"),
        ),
    ),
    *export_group(
        "orchestrator.stages._implementing_recovery",
        (
            ("_park_agent_timeout", "_park_agent_timeout"),
            ("_publish_committed_work", "_publish_committed_work"),
            ("_try_recover_implementing_timeout_park", "_try_recover_implementing_timeout_park"),
        ),
    ),
    *export_group(
        "orchestrator.stages._implementing_resume",
        (
            ("_DevResumeContext", "_DevResumeContext"),
            ("_DevResumeRequest", "_DevResumeRequest"),
            ("_ensure_resume_worktree", "_ensure_resume_worktree"),
            ("_resume_dev_with_text", "_resume_dev_with_text"),
            ("_resume_developer_on_human_reply", "_resume_developer_on_human_reply"),
        ),
    ),
    *export_group(
        "orchestrator.stages._implementing_session",
        (
            ("_build_dev_spawn_prompt", "_build_dev_spawn_prompt"),
            ("_check_and_increment_retry_budget", "_check_and_increment_retry_budget"),
            ("_dev_session_retirement_reason", "_dev_session_retirement_reason"),
            ("_drop_poisoned_dev_session", "_drop_poisoned_dev_session"),
            ("_is_poisoned_session_failure", "_is_poisoned_session_failure"),
            ("_persist_dev_session_after_run", "_persist_dev_session_after_run"),
            ("_resolve_dev_session_for_resume", "_resolve_dev_session_for_resume"),
        ),
    ),
    *export_group(
        "orchestrator.stages._implementing_session_read",
        (
            ("_as_blockquote", "_as_blockquote"),
            ("_is_context_overflow_failure", "_is_context_overflow_failure"),
            ("_is_session_limit_message", "_is_session_limit_message"),
            ("_is_stale_session_failure", "_is_stale_session_failure"),
            ("_read_dev_session", "_read_dev_session"),
            ("_stored_dev_session", "_stored_dev_session"),
        ),
    ),
    *export_group(
        "orchestrator.stages._implementing_spawn",
        (
            ("_prepare_active_dev_run", "_prepare_active_dev_run"),
            ("_prepare_dev_run", "_prepare_dev_run"),
            ("_recovered_dev_result", "_recovered_dev_result"),
            ("_spawn_implementer", "_spawn_implementer"),
        ),
    ),
    *export_group(
        "orchestrator.state_machine",
        (("WorkflowLabel", "WorkflowLabel"),),
    ),
    *export_group(
        "pathlib",
        (("Path", "Path"),),
    ),
    *export_group(
        "typing",
        (
            ("Optional", "Optional"),
            ("Tuple", "Tuple"),
        ),
    ),
)
EXPORTED_NAMES = None
