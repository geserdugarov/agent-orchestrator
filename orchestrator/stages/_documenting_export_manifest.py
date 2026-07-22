# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Immutable lazy-export inventory for :mod:`orchestrator.stages.documenting`."""

from __future__ import annotations

from orchestrator._compat_exports import export_group

EXPORTS = (
    *export_group(
        "contextlib",
        (("suppress", "suppress"),),
    ),
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
        "orchestrator.stages._documenting_drift",
        (
            ("_announce_documenting_drift", "_announce_documenting_drift"),
            ("_begin_documenting_drift_unwind", "_begin_documenting_drift_unwind"),
            ("_documenting_drift_fetch", "_documenting_drift_fetch"),
            ("_documenting_drift_hard_reset", "_documenting_drift_hard_reset"),
            ("_documenting_drift_probe", "_documenting_drift_probe"),
            ("_reconcile_documenting_drift", "_reconcile_documenting_drift"),
            ("_reset_documenting_drift_worktree", "_reset_documenting_drift_worktree"),
        ),
    ),
    *export_group(
        "orchestrator.stages._documenting_handler",
        (
            ("_drive_documenting_pass", "_drive_documenting_pass"),
            ("_handle_documenting", "_handle_documenting"),
        ),
    ),
    *export_group(
        "orchestrator.stages._documenting_models",
        (
            ("_DocumentingContext", "_DocumentingContext"),
            ("_DocumentingRun", "_DocumentingRun"),
            ("_advance_after_docs_no_change", "_advance_after_docs_no_change"),
            ("_advance_after_docs_push", "_advance_after_docs_push"),
            ("_park_documenting", "_park_documenting"),
            ("_ratchet_in_review_watermark_for_final_docs", "_ratchet_in_review_watermark_for_final_docs"),
        ),
    ),
    *export_group(
        "orchestrator.stages._documenting_outcomes",
        (
            ("_dispose_documenting_clean", "_dispose_documenting_clean"),
            ("_dispose_documenting_outcome", "_dispose_documenting_outcome"),
            ("_park_documenting_dirty", "_park_documenting_dirty"),
            ("_park_documenting_question", "_park_documenting_question"),
        ),
    ),
    *export_group(
        "orchestrator.stages._documenting_persistence",
        (
            ("_documenting_commit_notice", "_documenting_commit_notice"),
            ("_documenting_no_change_note", "_documenting_no_change_note"),
            ("_post_docs_notice", "_post_docs_notice"),
            ("_push_docs_and_advance", "_push_docs_and_advance"),
            ("_route_documenting_no_change", "_route_documenting_no_change"),
            ("_stamp_docs_verdict", "_stamp_docs_verdict"),
        ),
    ),
    *export_group(
        "orchestrator.stages._documenting_preconditions",
        (
            ("_documenting_parked_no_input", "_documenting_parked_no_input"),
            ("_documenting_preconditions_handled", "_documenting_preconditions_handled"),
            ("_finalize_documenting_terminal", "_finalize_documenting_terminal"),
            ("_park_documenting_without_pr", "_park_documenting_without_pr"),
            ("_refuse_parked_continue_command", "_refuse_parked_continue_command"),
        ),
    ),
    *export_group(
        "orchestrator.stages._documenting_run",
        (
            ("_documentation_prompt", "_documentation_prompt"),
            ("_fresh_documenting_run", "_fresh_documenting_run"),
            ("_prepare_documenting_worktree", "_prepare_documenting_worktree"),
            ("_recovered_documenting_run", "_recovered_documenting_run"),
            ("_resume_documenting_dev", "_resume_documenting_dev"),
            ("_run_documenting_dev", "_run_documenting_dev"),
        ),
    ),
    *export_group(
        "orchestrator.stages._documenting_state",
        (
            ("_AWAITING_HUMAN", "_AWAITING_HUMAN"),
            ("_LAST_ACTION_COMMENT_ID", "_LAST_ACTION_COMMENT_ID"),
            ("_PARK_REASON", "_PARK_REASON"),
        ),
    ),
    *export_group(
        "orchestrator.state_machine",
        (("WorkflowLabel", "WorkflowLabel"),),
    ),
    *export_group(
        "typing",
        (("Any", "Any"),),
    ),
)
EXPORTED_NAMES = None
