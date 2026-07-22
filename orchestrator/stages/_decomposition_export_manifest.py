# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Immutable lazy-export inventory for :mod:`orchestrator.stages.decomposition`."""

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
        "orchestrator.stages._decomposition_activation",
        (
            ("_ChildActivation", "_ChildActivation"),
            ("_activate_ready_children", "_activate_ready_children"),
            ("_held_dependency_line", "_held_dependency_line"),
            ("_log_held_children", "_log_held_children"),
        ),
    ),
    *export_group(
        "orchestrator.stages._decomposition_blocked",
        (
            ("_complete_blocked_parent", "_complete_blocked_parent"),
            ("_handle_blocked", "_handle_blocked"),
            ("_handle_empty_blocked_parent", "_handle_empty_blocked_parent"),
            ("_handle_ready", "_handle_ready"),
            ("_usable_child_scan", "_usable_child_scan"),
        ),
    ),
    *export_group(
        "orchestrator.stages._decomposition_models",
        (
            ("_ChildScan", "_ChildScan"),
            ("_DecomposerRunPlan", "_DecomposerRunPlan"),
            ("_DecomposerSession", "_DecomposerSession"),
            ("_SplitPlan", "_SplitPlan"),
        ),
    ),
    *export_group(
        "orchestrator.stages._decomposition_parent_scan",
        (
            ("_manually_closed_children", "_manually_closed_children"),
            ("_park_manually_closed_children", "_park_manually_closed_children"),
            ("_park_rejected_children", "_park_rejected_children"),
            ("_read_child_labels", "_read_child_labels"),
            ("_remaining_manually_closed", "_remaining_manually_closed"),
            ("_route_parent_drift", "_route_parent_drift"),
        ),
    ),
    *export_group(
        "orchestrator.stages._decomposition_recovery",
        (
            ("_finalize_single_decision", "_finalize_single_decision"),
            ("_park_unparsed_manifest", "_park_unparsed_manifest"),
            ("_recover_stale_manifest", "_recover_stale_manifest"),
            ("_route_disabled_to_implementing", "_route_disabled_to_implementing"),
            ("_spawn_fresh_decomposer", "_spawn_fresh_decomposer"),
        ),
    ),
    *export_group(
        "orchestrator.stages._decomposition_recovery_state",
        (
            ("_clear_decomposition_manifest", "_clear_decomposition_manifest"),
            ("_decomposition_drift_notice", "_decomposition_drift_notice"),
            ("_issue_ref_list", "_issue_ref_list"),
            ("_park_incomplete_decomposition", "_park_incomplete_decomposition"),
            ("_repair_recovered_child", "_repair_recovered_child"),
            ("_repair_recovered_children", "_repair_recovered_children"),
            ("_seed_orphan_child_state", "_seed_orphan_child_state"),
        ),
    ),
    *export_group(
        "orchestrator.stages._decomposition_run",
        (
            ("_dispatch_decomposer_manifest", "_dispatch_decomposer_manifest"),
            ("_handle_decomposing", "_handle_decomposing"),
            ("_prepare_decomposer_run", "_prepare_decomposer_run"),
            ("_process_decomposer_run", "_process_decomposer_run"),
            ("_settle_decomposer_run", "_settle_decomposer_run"),
        ),
    ),
    *export_group(
        "orchestrator.stages._decomposition_session",
        (
            ("_decomposer_followup", "_decomposer_followup"),
            ("_read_decomposer_session", "_read_decomposer_session"),
            ("_reset_decomposing_on_drift", "_reset_decomposing_on_drift"),
            ("_resume_decomposer_on_human_reply", "_resume_decomposer_on_human_reply"),
        ),
    ),
    *export_group(
        "orchestrator.stages._decomposition_split",
        (
            ("_activate_initial_split_children", "_activate_initial_split_children"),
            ("_create_child_issues", "_create_child_issues"),
            ("_finalize_split", "_finalize_split"),
            ("_split_summary", "_split_summary"),
        ),
    ),
    *export_group(
        "orchestrator.stages._decomposition_split_state",
        (
            ("_child_initial_labels", "_child_initial_labels"),
            ("_create_planned_child", "_create_planned_child"),
            ("_park_child_create_failure", "_park_child_create_failure"),
            ("_persist_created_child", "_persist_created_child"),
            ("_prepare_split_plan", "_prepare_split_plan"),
            ("_seed_created_child", "_seed_created_child"),
            ("_write_child_pinned_state", "_write_child_pinned_state"),
        ),
    ),
    *export_group(
        "orchestrator.stages._decomposition_state",
        (
            ("_AWAITING_HUMAN", "_AWAITING_HUMAN"),
            ("_CHILDREN", "_CHILDREN"),
            ("_CREATED_AT", "_CREATED_AT"),
            ("_DONE", "_DONE"),
            ("_HeldChild", "_HeldChild"),
            ("_LAST_ACTION_COMMENT_ID", "_LAST_ACTION_COMMENT_ID"),
            ("_PARENT_NUMBER", "_PARENT_NUMBER"),
            ("_PARK_REASON", "_PARK_REASON"),
            ("_UMBRELLA", "_UMBRELLA"),
        ),
    ),
    *export_group(
        "orchestrator.stages._decomposition_umbrella",
        (
            ("_complete_umbrella", "_complete_umbrella"),
            ("_handle_empty_umbrella", "_handle_empty_umbrella"),
            ("_handle_umbrella", "_handle_umbrella"),
        ),
    ),
    *export_group(
        "orchestrator.state_machine",
        (("WorkflowLabel", "WorkflowLabel"),),
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
