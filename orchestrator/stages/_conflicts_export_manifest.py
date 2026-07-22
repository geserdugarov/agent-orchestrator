# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Immutable lazy-export inventory for :mod:`orchestrator.stages.conflicts`."""

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
        "orchestrator.stages._conflict_divergence",
        (
            ("_guard_diverged_worktree", "_guard_diverged_worktree"),
            ("_park_diverged_worktree", "_park_diverged_worktree"),
            ("_push_recovered_commits", "_push_recovered_commits"),
            ("_still_behind_base", "_still_behind_base"),
        ),
    ),
    *export_group(
        "orchestrator.stages._conflict_guards",
        (
            ("_already_rebased_onto_base", "_already_rebased_onto_base"),
            ("_ensure_conflict_worktree", "_ensure_conflict_worktree"),
            ("_pr_head_orchestrator_produced", "_pr_head_orchestrator_produced"),
        ),
    ),
    *export_group(
        "orchestrator.stages._conflict_models",
        (
            ("_ConflictContext", "_ConflictContext"),
            ("_ConflictResumeRun", "_ConflictResumeRun"),
            ("_DivergeDecision", "_DivergeDecision"),
            ("_WorktreeSync", "_WorktreeSync"),
        ),
    ),
    *export_group(
        "orchestrator.stages._conflict_outcomes",
        (
            ("_finalize_conflict_resolution", "_finalize_conflict_resolution"),
            ("_park_stalled_conflict_result", "_park_stalled_conflict_result"),
            ("_post_conflict_resolution_result", "_post_conflict_resolution_result"),
        ),
    ),
    *export_group(
        "orchestrator.stages._conflict_publish",
        (
            ("_flip_base_up_to_date", "_flip_base_up_to_date"),
            ("_publish_clean_rebase", "_publish_clean_rebase"),
            ("_resolve_conflicts_with_agent", "_resolve_conflicts_with_agent"),
        ),
    ),
    *export_group(
        "orchestrator.stages._conflict_rebase",
        (
            ("_fetch_base_ref", "_fetch_base_ref"),
            ("_fetch_pr_branch", "_fetch_pr_branch"),
            ("_merge_result", "_merge_result"),
            ("_rebase_and_dispose", "_rebase_and_dispose"),
        ),
    ),
    *export_group(
        "orchestrator.stages._conflict_resume",
        (
            ("_awaiting_human_followup", "_awaiting_human_followup"),
            ("_resume_awaiting_human", "_resume_awaiting_human"),
            ("_resume_on_user_content_change", "_resume_on_user_content_change"),
            ("_run_conflict_resume", "_run_conflict_resume"),
        ),
    ),
    *export_group(
        "orchestrator.stages._conflict_routing",
        (
            ("_drive_conflict_rebase", "_drive_conflict_rebase"),
            ("_handle_resolving_conflict", "_handle_resolving_conflict"),
            ("_park_conflict_missing_pr_number", "_park_conflict_missing_pr_number"),
            ("_prepare_conflict_worktree", "_prepare_conflict_worktree"),
        ),
    ),
    *export_group(
        "orchestrator.stages._conflict_state",
        (
            ("_CONFLICT_ROUND", "_CONFLICT_ROUND"),
            ("_REVIEW_ROUND", "_REVIEW_ROUND"),
        ),
    ),
    *export_group(
        "orchestrator.stages._conflict_transitions",
        (
            ("_emit_conflict_round_incremented", "_emit_conflict_round_incremented"),
            ("_hand_resolved_round_to_validating", "_hand_resolved_round_to_validating"),
            ("_park_conflict", "_park_conflict"),
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
        (("Optional", "Optional"),),
    ),
)
EXPORTED_NAMES = None
