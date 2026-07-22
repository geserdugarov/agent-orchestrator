# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Immutable lazy-export inventory for :mod:`orchestrator.stages.fixing`."""

from __future__ import annotations

from orchestrator._compat_exports import export_group

EXPORTS = (
    *export_group(
        "dataclasses",
        (("dataclass", "dataclass"),),
    ),
    *export_group(
        "datetime",
        (
            ("datetime", "datetime"),
            ("timezone", "timezone"),
        ),
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
        "orchestrator.stages._fixing_bookmarks",
        (
            ("_clear_pending_fix_bookmarks", "_clear_pending_fix_bookmarks"),
            ("_pending_fix_id_set", "_pending_fix_id_set"),
            ("_reconstruct_issue_space", "_reconstruct_issue_space"),
            ("_reconstruct_review_comments", "_reconstruct_review_comments"),
            ("_reconstruct_review_summaries", "_reconstruct_review_summaries"),
            ("_reviewer_anchor_comment", "_reviewer_anchor_comment"),
        ),
    ),
    *export_group(
        "orchestrator.stages._fixing_continue",
        (
            ("_advance_consumed_watermarks", "_advance_consumed_watermarks"),
            ("_handle_continue_command", "_handle_continue_command"),
            ("_reconstruct_pending_fix_batch", "_reconstruct_pending_fix_batch"),
        ),
    ),
    *export_group(
        "orchestrator.stages._fixing_drift",
        (
            ("_fixing_drift_reason", "_fixing_drift_reason"),
            ("_post_fixing_conflict_notice", "_post_fixing_conflict_notice"),
            ("_reconcile_parked_fixing", "_reconcile_parked_fixing"),
            ("_route_parked_fixing_to_conflict", "_route_parked_fixing_to_conflict"),
            ("_stale_pr_head_reason", "_stale_pr_head_reason"),
        ),
    ),
    *export_group(
        "orchestrator.stages._fixing_feedback",
        (
            ("_new_issue_space_feedback", "_new_issue_space_feedback"),
            ("_new_review_comment_feedback", "_new_review_comment_feedback"),
            ("_new_review_summary_feedback", "_new_review_summary_feedback"),
            ("_rescan_fixing_feedback", "_rescan_fixing_feedback"),
        ),
    ),
    *export_group(
        "orchestrator.stages._fixing_models",
        (
            ("_FixingContext", "_FixingContext"),
            ("_FixingFeedback", "_FixingFeedback"),
            ("_FixingResumeRun", "_FixingResumeRun"),
            ("_ParkedFixingDecision", "_ParkedFixingDecision"),
            ("_fixing_preflight", "_fixing_preflight"),
            ("_park_fixing_without_pr", "_park_fixing_without_pr"),
        ),
    ),
    *export_group(
        "orchestrator.stages._fixing_parked",
        (
            ("_dispatch_continue_command", "_dispatch_continue_command"),
            ("_dispatch_parked_fixing", "_dispatch_parked_fixing"),
            ("_dispatch_validating_recovery", "_dispatch_validating_recovery"),
        ),
    ),
    *export_group(
        "orchestrator.stages._fixing_resume",
        (
            ("_apply_fix_review_round", "_apply_fix_review_round"),
            ("_fixing_ack_fast_path", "_fixing_ack_fast_path"),
            ("_fixing_debounce_open", "_fixing_debounce_open"),
            ("_handle_fixing", "_handle_fixing"),
            ("_resume_fixing_and_dispatch_result", "_resume_fixing_and_dispatch_result"),
            ("_run_fixing_resume", "_run_fixing_resume"),
        ),
    ),
    *export_group(
        "orchestrator.stages._fixing_state",
        (
            ("_AWAITING_HUMAN", "_AWAITING_HUMAN"),
            ("_CONFLICT_ROUND", "_CONFLICT_ROUND"),
            ("_PARK_REASON", "_PARK_REASON"),
            ("_PENDING_FIX_AT", "_PENDING_FIX_AT"),
            ("_REVIEW_ROUND", "_REVIEW_ROUND"),
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
            ("Any", "Any"),
            ("Optional", "Optional"),
        ),
    ),
)
EXPORTED_NAMES = None
