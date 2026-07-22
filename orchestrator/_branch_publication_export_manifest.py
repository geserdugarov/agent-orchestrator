# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Immutable lazy-export inventory for :mod:`orchestrator.branch_publication`."""

from __future__ import annotations

from orchestrator._compat_exports import export_group

EXPORTS = (
    *export_group(
        "collections",
        (("Counter", "Counter"),),
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
        "logging",
        (("logging", None),),
    ),
    *export_group(
        "orchestrator._workflow_dependencies",
        (("config", "config"),),
    ),
    *export_group(
        "orchestrator._branch_probes",
        (
            ("_branch_ahead_behind", "_branch_ahead_behind"),
            ("_first_commit_subject", "_first_commit_subject"),
            ("_is_conventional_subject", "_is_conventional_subject"),
            ("_is_prefixed_subject", "_is_prefixed_subject"),
            ("_parse_ahead_behind", "_parse_ahead_behind"),
            ("_recent_base_subjects", "_recent_base_subjects"),
            ("_subject_prefix", "_subject_prefix"),
        ),
    ),
    *export_group(
        "orchestrator._branch_publication_flow",
        (
            ("_infer_subject_prefix", "_infer_subject_prefix"),
            ("_pr_title_from_commit_or_issue", "_pr_title_from_commit_or_issue"),
            ("_squash_and_force_push", "_squash_and_force_push"),
        ),
    ),
    *export_group(
        "orchestrator._branch_publication_state",
        (
            ("_CONVENTIONAL_RE", "_CONVENTIONAL_RE"),
            ("_CONVENTIONAL_TYPES", "_CONVENTIONAL_TYPES"),
            ("_CONVENTIONAL_TYPES_ALT", "_CONVENTIONAL_TYPES_ALT"),
            ("_PREFIXED_RE", "_PREFIXED_RE"),
            ("_PREFIX_TOKEN_RE", "_PREFIX_TOKEN_RE"),
            ("log", "log"),
        ),
    ),
    *export_group(
        "orchestrator._branch_squash_plan",
        (
            ("_SquashPlan", "_SquashPlan"),
            ("_SquashPreparationError", "_SquashPreparationError"),
            ("_prepare_squash", "_prepare_squash"),
            ("_squash_base_sha", "_squash_base_sha"),
            ("_squash_message", "_squash_message"),
            ("_squash_subjects", "_squash_subjects"),
        ),
    ),
    *export_group(
        "orchestrator._branch_squash_rewrite",
        (
            ("_create_squash_commit", "_create_squash_commit"),
            ("_rewrite_squash", "_rewrite_squash"),
            ("_rollback_squash", "_rollback_squash"),
            ("_squash_commit_env", "_squash_commit_env"),
            ("_squash_failure", "_squash_failure"),
        ),
    ),
    *export_group(
        "orchestrator.git_plumbing",
        (
            ("_GIT_NO_PROMPT_ENV", "_GIT_NO_PROMPT_ENV"),
            ("_git", "_git"),
            ("_git_hardened", "_git_hardened"),
            ("_push_branch", "_push_branch"),
        ),
    ),
    *export_group(
        "orchestrator.verify",
        (
            ("_head_sha", "_head_sha"),
            ("_worktree_dirty_files", "_worktree_dirty_files"),
        ),
    ),
    *export_group(
        "os",
        (("os", None),),
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
        (
            ("List", "List"),
            ("Optional", "Optional"),
            ("Tuple", "Tuple"),
        ),
    ),
)
EXPORTED_NAMES = None
