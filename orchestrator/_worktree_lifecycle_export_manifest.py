# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Immutable lazy-export inventory for :mod:`orchestrator.worktree_lifecycle`."""

from __future__ import annotations

from orchestrator._compat_exports import export_group

EXPORTS = (
    *export_group(
        "hashlib",
        (("hashlib", None),),
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
        "orchestrator._worktree_cleanup",
        (
            ("_delete_local_issue_branch", "_delete_local_issue_branch"),
            ("_remove_issue_worktree", "_remove_issue_worktree"),
            ("_run_issue_worktree_removal", "_run_issue_worktree_removal"),
            ("_run_local_branch_deletion", "_run_local_branch_deletion"),
        ),
    ),
    *export_group(
        "orchestrator._worktree_creation",
        (
            ("_commit_count_from_stdout", "_commit_count_from_stdout"),
            ("_ensure_pr_worktree", "_ensure_pr_worktree"),
            ("_ensure_worktree", "_ensure_worktree"),
            ("_has_new_commits", "_has_new_commits"),
        ),
    ),
    *export_group(
        "orchestrator._worktree_decomposition",
        (
            ("_cleanup_decompose_worktree", "_cleanup_decompose_worktree"),
            ("_decompose_worktree_path", "_decompose_worktree_path"),
            ("_ensure_decompose_worktree", "_ensure_decompose_worktree"),
            ("_run_decompose_worktree_removal", "_run_decompose_worktree_removal"),
        ),
    ),
    *export_group(
        "orchestrator._worktree_lifecycle_state",
        (
            ("_SAFE_CHAR", "_SAFE_CHAR"),
            ("_SLUG_DIGEST_LEN", "_SLUG_DIGEST_LEN"),
            ("_SLUG_SAFE_RE", "_SLUG_SAFE_RE"),
            ("log", "log"),
        ),
    ),
    *export_group(
        "orchestrator._worktree_paths",
        (
            ("_branch_name", "_branch_name"),
            ("_repo_worktrees_root", "_repo_worktrees_root"),
            ("_resolve_branch_name", "_resolve_branch_name"),
            ("_sanitize_branch_segment", "_sanitize_branch_segment"),
            ("_sanitize_slug", "_sanitize_slug"),
            ("_slug_digest", "_slug_digest"),
            ("_worktree_path", "_worktree_path"),
        ),
    ),
    *export_group(
        "orchestrator._worktree_recovery",
        (
            ("_branch_commit_count", "_branch_commit_count"),
            ("_branch_has_unpushed_commits", "_branch_has_unpushed_commits"),
            ("_candidate_issue_branches", "_candidate_issue_branches"),
        ),
    ),
    *export_group(
        "orchestrator._worktree_terminal",
        (
            ("_cleanup_question_worktree", "_cleanup_question_worktree"),
            ("_cleanup_terminal_branch", "_cleanup_terminal_branch"),
        ),
    ),
    *export_group(
        "orchestrator.git_plumbing",
        (
            ("_authed_target_fetch", "_authed_target_fetch"),
            ("_git", "_git"),
            ("_target_root_lock", "_target_root_lock"),
        ),
    ),
    *export_group(
        "orchestrator.github",
        (
            ("GitHubClient", "GitHubClient"),
            ("PinnedState", "PinnedState"),
        ),
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
        "subprocess",
        (("subprocess", None),),
    ),
    *export_group(
        "typing",
        (("Optional", "Optional"),),
    ),
)
EXPORTED_NAMES = None
