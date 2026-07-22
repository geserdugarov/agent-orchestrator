# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Immutable lazy-export inventory for :mod:`orchestrator.workflow_drift`."""

from __future__ import annotations

from orchestrator._compat_exports import export_group

EXPORTS = (
    *export_group(
        "github.Issue",
        (("Issue", "Issue"),),
    ),
    *export_group(
        "github.IssueComment",
        (("IssueComment", "IssueComment"),),
    ),
    *export_group(
        "hashlib",
        (("hashlib", None),),
    ),
    *export_group(
        "orchestrator._workflow_drift_hash",
        (
            ("_comment_body_for_hash", "_comment_body_for_hash"),
            ("_compute_user_content_hash", "_compute_user_content_hash"),
            ("_detect_user_content_change", "_detect_user_content_change"),
            ("_is_hidden_comment", "_is_hidden_comment"),
        ),
    ),
    *export_group(
        "orchestrator._workflow_drift_routes",
        (
            ("_build_user_content_change_prompt", "_build_user_content_change_prompt"),
            ("_drift_to_decomposing_notice", "_drift_to_decomposing_notice"),
            ("_mark_drift_comments_consumed", "_mark_drift_comments_consumed"),
            ("_reset_decomposition_for_drift", "_reset_decomposition_for_drift"),
            ("_route_drift_to_decomposing", "_route_drift_to_decomposing"),
        ),
    ),
    *export_group(
        "orchestrator._workflow_drift_state",
        (("_USER_CONTENT_HASH", "_USER_CONTENT_HASH"),),
    ),
    *export_group(
        "orchestrator.comment_trust",
        (("is_trusted_author", "is_trusted_author"),),
    ),
    *export_group(
        "orchestrator.github",
        (
            ("GitHubClient", "GitHubClient"),
            ("PINNED_STATE_MARKER", "PINNED_STATE_MARKER"),
            ("PinnedState", "PinnedState"),
        ),
    ),
    *export_group(
        "orchestrator.state_machine",
        (("WorkflowLabel", "WorkflowLabel"),),
    ),
    *export_group(
        "orchestrator.workflow_messages",
        (
            ("_COMMIT_STYLE_NOTE", "_COMMIT_STYLE_NOTE"),
            ("_FOREGROUND_ONLY_NOTE", "_FOREGROUND_ONLY_NOTE"),
            ("_ORCH_COMMENT_MARKER", "_ORCH_COMMENT_MARKER"),
            ("_as_blockquote", "_as_blockquote"),
            ("_is_bare_orchestrator_continue", "_is_bare_orchestrator_continue"),
            ("_orchestrator_ids", "_orchestrator_ids"),
            ("_post_issue_comment", "_post_issue_comment"),
        ),
    ),
    *export_group(
        "typing",
        (("Optional", "Optional"),),
    ),
)
EXPORTED_NAMES = None
