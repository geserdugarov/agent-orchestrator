# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Immutable lazy-export inventory for :mod:`orchestrator.base_sync`."""

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
        "github.PullRequest",
        (("PullRequest", "PullRequest"),),
    ),
    *export_group(
        "logging",
        (("logging", None),),
    ),
    *export_group(
        "orchestrator._workflow_dependencies",
        (("config", "config"),),
    ),
    *export_group(
        "orchestrator._base_sync_eligibility",
        (
            ("_auto_rebase_label_is_eligible", "_auto_rebase_label_is_eligible"),
            ("_auto_rebase_recovery_decision", "_auto_rebase_recovery_decision"),
            ("_auto_rebase_retry_decision", "_auto_rebase_retry_decision"),
            ("_normal_auto_rebase_can_start", "_normal_auto_rebase_can_start"),
            ("_open_auto_rebase_pr", "_open_auto_rebase_pr"),
        ),
    ),
    *export_group(
        "orchestrator._base_sync_models",
        (
            ("_AutoRebaseContext", "_AutoRebaseContext"),
            ("_AutoRebaseDecision", "_AutoRebaseDecision"),
            ("_AutoRebaseRequest", "_AutoRebaseRequest"),
            ("_AutoRebaseRecoveryContext", "_AutoRebaseRecoveryContext"),
            ("_AutoRebaseRecoverySnapshot", "_AutoRebaseRecoverySnapshot"),
            ("_ConflictRouteContext", "_ConflictRouteContext"),
        ),
    ),
    *export_group(
        "orchestrator._base_sync_pr",
        (
            ("_publish_auto_rebase_from_pr", "_publish_auto_rebase_from_pr"),
            ("_route_pr_worktree_conflict_context", "_route_pr_worktree_conflict_context"),
            ("_route_pr_worktree_to_resolving_conflict", "_route_pr_worktree_to_resolving_conflict"),
            ("_sync_pr_worktree_context", "_sync_pr_worktree_context"),
            ("_sync_pr_worktree_to_base", "_sync_pr_worktree_to_base"),
        ),
    ),
    *export_group(
        "orchestrator._base_sync_pre_pr",
        (
            ("_base_sync_issue", "_base_sync_issue"),
            ("_issue_skips_base_sync", "_issue_skips_base_sync"),
            ("_sync_pre_pr_worktree", "_sync_pre_pr_worktree"),
            ("_sync_worktree_with_base", "_sync_worktree_with_base"),
            ("_worktree_behind_base", "_worktree_behind_base"),
        ),
    ),
    *export_group(
        "orchestrator._base_sync_publish",
        (
            ("_emit_auto_rebase_event", "_emit_auto_rebase_event"),
            ("_finalize_auto_rebase", "_finalize_auto_rebase"),
            ("_post_auto_rebase_notice", "_post_auto_rebase_notice"),
            ("_publish_auto_rebase", "_publish_auto_rebase"),
        ),
    ),
    *export_group(
        "orchestrator._base_sync_publish_guards",
        (
            ("_finish_noop_auto_rebase", "_finish_noop_auto_rebase"),
            ("_park_dirty_auto_rebase", "_park_dirty_auto_rebase"),
            ("_park_failed_auto_rebase_push", "_park_failed_auto_rebase_push"),
            ("_park_unreadable_post_rebase_head", "_park_unreadable_post_rebase_head"),
        ),
    ),
    *export_group(
        "orchestrator._base_sync_recovery",
        (
            ("_recover_pending_auto_base_rebase", "_recover_pending_auto_base_rebase"),
            ("_recover_pending_auto_base_rebase_context", "_recover_pending_auto_base_rebase_context"),
            ("_retry_recovery_push", "_retry_recovery_push"),
            ("_route_recovery_snapshot", "_route_recovery_snapshot"),
        ),
    ),
    *export_group(
        "orchestrator._base_sync_recovery_decisions",
        (
            ("_already_published_recovery_notice", "_already_published_recovery_notice"),
            ("_finalize_already_published_recovery", "_finalize_already_published_recovery"),
            ("_park_dirty_recovery", "_park_dirty_recovery"),
            ("_park_diverged_recovery", "_park_diverged_recovery"),
            ("_park_failed_recovery_push", "_park_failed_recovery_push"),
            ("_pushed_recovery_notice", "_pushed_recovery_notice"),
            ("_reject_unknown_recovery_comparison", "_reject_unknown_recovery_comparison"),
        ),
    ),
    *export_group(
        "orchestrator._base_sync_recovery_persistence",
        (
            ("_emit_recovered_rebase_event", "_emit_recovered_rebase_event"),
            ("_finalize_recovered_rebase", "_finalize_recovered_rebase"),
            ("_park_auto_rebase_failure", "_park_auto_rebase_failure"),
            ("_post_recovered_rebase_notice", "_post_recovered_rebase_notice"),
            ("_prepare_recovered_rebase_state", "_prepare_recovered_rebase_state"),
            ("_reset_clear_and_park", "_reset_clear_and_park"),
            ("_route_recovered_rebase", "_route_recovered_rebase"),
        ),
    ),
    *export_group(
        "orchestrator._base_sync_recovery_snapshot",
        (
            ("_abort_recovery_unverified", "_abort_recovery_unverified"),
            ("_clear_ineligible_recovery", "_clear_ineligible_recovery"),
            ("_clear_unchanged_recovery", "_clear_unchanged_recovery"),
            ("_complete_recovery_snapshot", "_complete_recovery_snapshot"),
            ("_fetch_recovery_snapshot", "_fetch_recovery_snapshot"),
            ("_read_remote_recovery_head", "_read_remote_recovery_head"),
        ),
    ),
    *export_group(
        "orchestrator._base_sync_refresh",
        (
            ("_issue_worktree_number", "_issue_worktree_number"),
            ("_merge_base_into_worktree", "_merge_base_into_worktree"),
            ("_rebase_base_into_worktree", "_rebase_base_into_worktree"),
            ("_rebase_in_progress", "_rebase_in_progress"),
            ("_rebase_state_exists", "_rebase_state_exists"),
            ("_refresh_base_and_worktrees", "_refresh_base_and_worktrees"),
            ("_sync_discovered_worktree", "_sync_discovered_worktree"),
        ),
    ),
    *export_group(
        "orchestrator._base_sync_start",
        (
            ("_handle_failed_auto_rebase", "_handle_failed_auto_rebase"),
            ("_park_unreadable_pre_rebase_head", "_park_unreadable_pre_rebase_head"),
            ("_record_auto_rebase_attempt", "_record_auto_rebase_attempt"),
            ("_start_auto_rebase", "_start_auto_rebase"),
        ),
    ),
    *export_group(
        "orchestrator._base_sync_state",
        (
            ("_AUTO_REBASE_PARK_REASONS", "_AUTO_REBASE_PARK_REASONS"),
            ("_AWAITING_HUMAN", "_AWAITING_HUMAN"),
            ("_CONFLICT_ROUND", "_CONFLICT_ROUND"),
            ("_ERROR_SNIPPET_LEN", "_ERROR_SNIPPET_LEN"),
            ("_PARK_REASON", "_PARK_REASON"),
            ("_PENDING_PUSH_SHA", "_PENDING_PUSH_SHA"),
            ("_PR_REFRESH_DETOUR_LABELS", "_PR_REFRESH_DETOUR_LABELS"),
            ("_REASON_AUTO_BASE_REBASE_FAILED", "_REASON_AUTO_BASE_REBASE_FAILED"),
            ("_REASON_AUTO_BASE_REBASE_PUSH_FAILED", "_REASON_AUTO_BASE_REBASE_PUSH_FAILED"),
            ("_REVIEW_ROUND", "_REVIEW_ROUND"),
            ("log", "log"),
        ),
    ),
    *export_group(
        "orchestrator.branch_publication",
        (("_branch_ahead_behind", "_branch_ahead_behind"),),
    ),
    *export_group(
        "orchestrator.comment_trust",
        (("filter_trusted", "filter_trusted"),),
    ),
    *export_group(
        "orchestrator.git_plumbing",
        (
            ("_authed_fetch", "_authed_fetch"),
            ("_authed_target_fetch", "_authed_target_fetch"),
            ("_git", "_git"),
            ("_git_hardened", "_git_hardened"),
            ("_push_branch", "_push_branch"),
        ),
    ),
    *export_group(
        "orchestrator.github",
        (
            ("GitHubClient", "GitHubClient"),
            ("PinnedState", "PinnedState"),
            ("hard_skip_control_label", "hard_skip_control_label"),
            ("issue_has_label", "issue_has_label"),
        ),
    ),
    *export_group(
        "orchestrator.scheduler",
        (("IssueScheduler", "IssueScheduler"),),
    ),
    *export_group(
        "orchestrator.state_machine",
        (("WorkflowLabel", "WorkflowLabel"),),
    ),
    *export_group(
        "orchestrator.verify",
        (
            ("_head_sha", "_head_sha"),
            ("_worktree_dirty_files", "_worktree_dirty_files"),
        ),
    ),
    *export_group(
        "orchestrator.workflow_messages",
        (("_post_pr_comment", "_post_pr_comment"),),
    ),
    *export_group(
        "orchestrator.worktree_lifecycle",
        (
            ("_repo_worktrees_root", "_repo_worktrees_root"),
            ("_resolve_branch_name", "_resolve_branch_name"),
        ),
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
