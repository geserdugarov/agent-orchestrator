# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Immutable lazy-export inventory for :mod:`orchestrator.stages.validating`."""

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
        "orchestrator.stages._validating_approval",
        (
            ("_approved_work_verifies", "_approved_work_verifies"),
            ("_finalize_validating_approval", "_finalize_validating_approval"),
            ("_park_squash_failure", "_park_squash_failure"),
            ("_post_approval_comment", "_post_approval_comment"),
            ("_seed_in_review_handoff_watermarks", "_seed_in_review_handoff_watermarks"),
            ("_seed_in_review_pr_watermarks", "_seed_in_review_pr_watermarks"),
            ("_squash_approved_work", "_squash_approved_work"),
        ),
    ),
    *export_group(
        "orchestrator.stages._validating_awaiting",
        (
            ("_AwaitingDevAttempt", "_AwaitingDevAttempt"),
            ("_AwaitingValidation", "_AwaitingValidation"),
            ("_resume_awaiting_dev_agent", "_resume_awaiting_dev_agent"),
            ("_review_cap_awaiting_action", "_review_cap_awaiting_action"),
            ("_reviewer_retry_awaiting_action", "_reviewer_retry_awaiting_action"),
            ("_run_awaiting_dev", "_run_awaiting_dev"),
            ("_transient_awaiting_action", "_transient_awaiting_action"),
        ),
    ),
    *export_group(
        "orchestrator.stages._validating_awaiting_handler",
        (
            ("_handle_validating_awaiting_human", "_handle_validating_awaiting_human"),
            ("_resume_validating_awaiting_dev", "_resume_validating_awaiting_dev"),
        ),
    ),
    *export_group(
        "orchestrator.stages._validating_dev_fix",
        (
            ("_dev_fix_is_publishable", "_dev_fix_is_publishable"),
            ("_dispose_dev_fix_result", "_dispose_dev_fix_result"),
            ("_handle_dev_fix_result", "_handle_dev_fix_result"),
            ("_park_dev_fix_timeout", "_park_dev_fix_timeout"),
            ("_publish_dev_fix", "_publish_dev_fix"),
            ("_stranded_fix_unpushed", "_stranded_fix_unpushed"),
        ),
    ),
    *export_group(
        "orchestrator.stages._validating_drift",
        (
            ("_ValidatingDriftRun", "_ValidatingDriftRun"),
            ("_defer_validating_drift", "_defer_validating_drift"),
            ("_finish_validating_drift", "_finish_validating_drift"),
            ("_resume_dev_on_validating_drift", "_resume_dev_on_validating_drift"),
            ("_run_validating_drift", "_run_validating_drift"),
        ),
    ),
    *export_group(
        "orchestrator.stages._validating_drift_result",
        (
            ("_bump_review_round", "_bump_review_round"),
            ("_dispose_user_content_change_result", "_dispose_user_content_change_result"),
            ("_post_drift_ack", "_post_drift_ack"),
            ("_post_user_content_change_result", "_post_user_content_change_result"),
            ("_recover_failed_push", "_recover_failed_push"),
            ("_recover_timed_out_fix", "_recover_timed_out_fix"),
            ("_try_recover_validating_transient_park", "_try_recover_validating_transient_park"),
        ),
    ),
    *export_group(
        "orchestrator.stages._validating_handler",
        (
            ("_dispatch_reviewer_result", "_dispatch_reviewer_result"),
            ("_handle_validating", "_handle_validating"),
            ("_run_reviewer_round", "_run_reviewer_round"),
        ),
    ),
    *export_group(
        "orchestrator.stages._validating_models",
        (
            ("_DevFixRun", "_DevFixRun"),
            ("_RequestedChanges", "_RequestedChanges"),
            ("_ReviewerDecision", "_ReviewerDecision"),
            ("_ReviewerRun", "_ReviewerRun"),
            ("_dev_fix_run", "_dev_fix_run"),
            ("_parse_add_review_rounds", "_parse_add_review_rounds"),
        ),
    ),
    *export_group(
        "orchestrator.stages._validating_requested_changes",
        (
            ("_finish_requested_fix", "_finish_requested_fix"),
            ("_handle_validating_changes_requested", "_handle_validating_changes_requested"),
            ("_park_review_cap", "_park_review_cap"),
            ("_park_reviewer_no_verdict", "_park_reviewer_no_verdict"),
            ("_post_reviewer_feedback", "_post_reviewer_feedback"),
            ("_run_requested_fix", "_run_requested_fix"),
        ),
    ),
    *export_group(
        "orchestrator.stages._validating_state",
        (
            ("_ADD_REVIEW_ROUNDS_RE", "_ADD_REVIEW_ROUNDS_RE"),
            ("_OUTCOME_PARKED", "_OUTCOME_PARKED"),
            ("_OUTCOME_PUSHED", "_OUTCOME_PUSHED"),
            ("_OUTCOME_RETURN", "_OUTCOME_RETURN"),
            ("_OUTCOME_STUCK", "_OUTCOME_STUCK"),
            ("_PARK_REASON", "_PARK_REASON"),
            ("_PRE_DEV_FIX_SHA", "_PRE_DEV_FIX_SHA"),
            ("_REASON_AGENT_TIMEOUT", "_REASON_AGENT_TIMEOUT"),
            ("_REASON_PUSH_FAILED", "_REASON_PUSH_FAILED"),
            ("_REASON_REVIEWER_FAILED", "_REASON_REVIEWER_FAILED"),
            ("_REASON_REVIEWER_TIMEOUT", "_REASON_REVIEWER_TIMEOUT"),
            ("_REASON_REVIEW_CAP", "_REASON_REVIEW_CAP"),
            ("_REVIEW_ROUND", "_REVIEW_ROUND"),
            ("_ReviewRoundsCommand", "_ReviewRoundsCommand"),
            ("_SHORT_SHA_LEN", "_SHORT_SHA_LEN"),
            ("_VALIDATING_TRANSIENT_PARK_REASONS", "_VALIDATING_TRANSIENT_PARK_REASONS"),
            ("_VERIFY_STATUS_TO_REASON", "_VERIFY_STATUS_TO_REASON"),
        ),
    ),
    *export_group(
        "orchestrator.stages._validating_verify",
        (
            ("_finalize_validating_terminal", "_finalize_validating_terminal"),
            ("_park_verify_failure", "_park_verify_failure"),
            ("_ratchet_watermark", "_ratchet_watermark"),
            ("_verify_failure_detail", "_verify_failure_detail"),
        ),
    ),
    *export_group(
        "orchestrator.stages._validating_watermarks",
        (
            ("_WatermarkWalker", "_WatermarkWalker"),
            ("_is_orchestrator_comment", "_is_orchestrator_comment"),
            ("_latest_pr_comment_ids", "_latest_pr_comment_ids"),
            ("_seed_watermark_past_self", "_seed_watermark_past_self"),
            ("_state_int", "_state_int"),
            ("_watermark_comment_pairs", "_watermark_comment_pairs"),
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
        "re",
        (("re", None),),
    ),
    *export_group(
        "types",
        (("MappingProxyType", "MappingProxyType"),),
    ),
    *export_group(
        "typing",
        (
            ("Any", "Any"),
            ("Optional", "Optional"),
            ("Tuple", "Tuple"),
        ),
    ),
)
EXPORTED_NAMES = None
