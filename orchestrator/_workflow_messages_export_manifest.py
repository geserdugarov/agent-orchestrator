# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Immutable lazy-export inventory for :mod:`orchestrator.workflow_messages`."""

from __future__ import annotations

from orchestrator._compat_exports import export_group

EXPORTS = (
    *export_group(
        "github.Issue",
        (("Issue", "Issue"),),
    ),
    *export_group(
        "json",
        (("json", None),),
    ),
    *export_group(
        "orchestrator._workflow_dependencies",
        (("config", "config"),),
    ),
    *export_group(
        "orchestrator._workflow_comments",
        (
            ("_build_tracked_repos_context", "_build_tracked_repos_context"),
            ("_orchestrator_ids", "_orchestrator_ids"),
            ("_post_issue_comment", "_post_issue_comment"),
            ("_post_pr_comment", "_post_pr_comment"),
            ("_track_orchestrator_comment", "_track_orchestrator_comment"),
            ("_with_orch_marker", "_with_orch_marker"),
        ),
    ),
    *export_group(
        "orchestrator._workflow_core_prompts",
        (
            ("_build_documentation_prompt", "_build_documentation_prompt"),
            ("_build_fix_prompt", "_build_fix_prompt"),
            ("_build_fresh_respawn_preamble", "_build_fresh_respawn_preamble"),
            ("_build_implement_prompt", "_build_implement_prompt"),
            ("_build_review_prompt", "_build_review_prompt"),
        ),
    ),
    *export_group(
        "orchestrator._workflow_decompose_prompts",
        (
            ("_build_decompose_prompt", "_build_decompose_prompt"),
            ("_build_single_decision_comment", "_build_single_decision_comment"),
            ("_single_manifest_files", "_single_manifest_files"),
            ("_single_manifest_text", "_single_manifest_text"),
        ),
    ),
    *export_group(
        "orchestrator._workflow_diagnostics",
        (
            ("_as_blockquote", "_as_blockquote"),
            ("_format_stderr_diagnostics", "_format_stderr_diagnostics"),
            ("_stderr_log_tail", "_stderr_log_tail"),
        ),
    ),
    *export_group(
        "orchestrator._workflow_manifest_cycles",
        (
            ("_dep_cycle_visit", "_dep_cycle_visit"),
            ("_has_dep_cycle", "_has_dep_cycle"),
        ),
    ),
    *export_group(
        "orchestrator._workflow_manifest_fields",
        (
            ("_decode_manifest", "_decode_manifest"),
            ("_extract_manifest_payload", "_extract_manifest_payload"),
            ("_is_nonempty_text", "_is_nonempty_text"),
            ("_manifest_child_dependencies", "_manifest_child_dependencies"),
            ("_manifest_child_text_error", "_manifest_child_text_error"),
            ("_manifest_umbrella_error", "_manifest_umbrella_error"),
            ("_split_manifest_children", "_split_manifest_children"),
        ),
    ),
    *export_group(
        "orchestrator._workflow_manifest_validation",
        (
            ("_is_valid_dependency", "_is_valid_dependency"),
            ("_manifest_child_error", "_manifest_child_error"),
            ("_manifest_children_error", "_manifest_children_error"),
            ("_manifest_validation_error", "_manifest_validation_error"),
            ("_parse_manifest", "_parse_manifest"),
            ("_split_manifest_error", "_split_manifest_error"),
        ),
    ),
    *export_group(
        "orchestrator._workflow_messages_state",
        (
            ("_COMMIT_STYLE_NOTE", "_COMMIT_STYLE_NOTE"),
            ("_CONTINUE_NEEDS_GUIDANCE_MSG", "_CONTINUE_NEEDS_GUIDANCE_MSG"),
            ("_CONTINUE_PARK_REASONS", "_CONTINUE_PARK_REASONS"),
            ("_CONTINUE_RETRY_PROMPT", "_CONTINUE_RETRY_PROMPT"),
            ("_DOC_VERDICT_RE", "_DOC_VERDICT_RE"),
            ("_DRIFT_ACK_RE", "_DRIFT_ACK_RE"),
            ("_FOREGROUND_ONLY_NOTE", "_FOREGROUND_ONLY_NOTE"),
            ("_MANIFEST_RE", "_MANIFEST_RE"),
            ("_MAX_CHILDREN", "_MAX_CHILDREN"),
            ("_MAX_FILES_SHOWN", "_MAX_FILES_SHOWN"),
            ("_NO_BODY", "_NO_BODY"),
            ("_NO_PRIOR_COMMENTS", "_NO_PRIOR_COMMENTS"),
            ("_ORCHESTRATOR_CONTINUE_RE", "_ORCHESTRATOR_CONTINUE_RE"),
            ("_ORCH_COMMENT_ID_CAP", "_ORCH_COMMENT_ID_CAP"),
            ("_ORCH_COMMENT_MARKER", "_ORCH_COMMENT_MARKER"),
            ("_REDACT_MIN_VALUE_LEN", "_REDACT_MIN_VALUE_LEN"),
            ("_SECRET_KEY_NAMES", "_SECRET_KEY_NAMES"),
            ("_SECRET_KEY_SUFFIXES", "_SECRET_KEY_SUFFIXES"),
            ("_SECTION_SEP", "_SECTION_SEP"),
            ("_STDERR_TAIL_BUDGET", "_STDERR_TAIL_BUDGET"),
            ("_TRACKED_REPOS_CAP", "_TRACKED_REPOS_CAP"),
            ("_VERDICT_RE", "_VERDICT_RE"),
            ("_VERDICT_UNKNOWN", "_VERDICT_UNKNOWN"),
        ),
    ),
    *export_group(
        "orchestrator._workflow_prompt_comments",
        (
            ("_prompt_comment_chunk", "_prompt_comment_chunk"),
            ("_quote_comment_line", "_quote_comment_line"),
            ("_recent_comments_text", "_recent_comments_text"),
        ),
    ),
    *export_group(
        "orchestrator._workflow_redaction",
        (
            ("_is_secret_environment_value", "_is_secret_environment_value"),
            ("_redact_configured_github_token", "_redact_configured_github_token"),
            ("_redact_environment_secrets", "_redact_environment_secrets"),
            ("_redact_secrets", "_redact_secrets"),
        ),
    ),
    *export_group(
        "orchestrator._workflow_stage_prompts",
        (
            ("_build_conflict_resolution_prompt", "_build_conflict_resolution_prompt"),
            ("_build_pr_comment_followup", "_build_pr_comment_followup"),
            ("_build_question_followup_prompt", "_build_question_followup_prompt"),
            ("_build_question_prompt", "_build_question_prompt"),
        ),
    ),
    *export_group(
        "orchestrator._workflow_verdicts",
        (
            ("_continue_command_action", "_continue_command_action"),
            ("_drift_ack_reason", "_drift_ack_reason"),
            ("_is_bare_orchestrator_continue", "_is_bare_orchestrator_continue"),
            ("_parse_documentation_verdict", "_parse_documentation_verdict"),
            ("_parse_orchestrator_continue", "_parse_orchestrator_continue"),
            ("_parse_review_verdict", "_parse_review_verdict"),
            ("_refuse_parked_continue", "_refuse_parked_continue"),
        ),
    ),
    *export_group(
        "orchestrator.agents",
        (("AgentResult", "AgentResult"),),
    ),
    *export_group(
        "orchestrator.comment_trust",
        (("is_trusted_author", "is_trusted_author"),),
    ),
    *export_group(
        "orchestrator.github",
        (
            ("GitHubClient", "GitHubClient"),
            ("PinnedState", "PinnedState"),
        ),
    ),
    *export_group(
        "os",
        (("os", None),),
    ),
    *export_group(
        "re",
        (("re", None),),
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
