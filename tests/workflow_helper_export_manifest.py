# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Immutable lazy-export inventory for shared workflow test helpers."""
from __future__ import annotations

from orchestrator._compat_exports import export_group


EXPORTS = (
    *export_group(
        "tests.workflow_conflict_support",
        (("_ResolvingConflictMixin", "_ResolvingConflictMixin"),),
    ),
    *export_group(
        "tests.workflow_event_values",
        (
            ("EVENT_AGENT_EXIT", "EVENT_AGENT_EXIT"),
            ("EVENT_AGENT_SPAWN", "EVENT_AGENT_SPAWN"),
            ("EVENT_AGENT_TRAJECTORY", "EVENT_AGENT_TRAJECTORY"),
            (
                "EVENT_PR_CLOSED_WITHOUT_MERGE",
                "EVENT_PR_CLOSED_WITHOUT_MERGE",
            ),
            ("EVENT_PR_MERGED", "EVENT_PR_MERGED"),
            ("EVENT_SKILL_TRIGGERED", "EVENT_SKILL_TRIGGERED"),
            ("EVENT_STAGE_ENTER", "EVENT_STAGE_ENTER"),
            ("EVENT_STAGE_EVALUATION", "EVENT_STAGE_EVALUATION"),
        ),
    ),
    *export_group(
        "tests.workflow_git_helpers",
        (
            ("_GitRunRecorder", "_GitRunRecorder"),
            (
                "_temp_git_repo_with_local_config",
                "_temp_git_repo_with_local_config",
            ),
            ("_TokenResolver", "_TokenResolver"),
        ),
    ),
    *export_group(
        "tests.workflow_other_labels",
        (
            ("LABEL_BLOCKED", "LABEL_BLOCKED"),
            ("LABEL_DONE", "LABEL_DONE"),
            ("LABEL_READY", "LABEL_READY"),
            ("LABEL_REJECTED", "LABEL_REJECTED"),
            (
                "LABEL_RESOLVING_CONFLICT",
                "LABEL_RESOLVING_CONFLICT",
            ),
            ("LABEL_UMBRELLA", "LABEL_UMBRELLA"),
        ),
    ),
    *export_group(
        "tests.workflow_patch_models",
        (("_agent", "_agent"),),
    ),
    *export_group(
        "tests.workflow_patch_runner",
        (("_PatchedWorkflowMixin", "_PatchedWorkflowMixin"),),
    ),
    *export_group(
        "tests.workflow_repo_values",
        (
            ("BACKEND_CLAUDE", "BACKEND_CLAUDE"),
            ("BACKEND_CODEX", "BACKEND_CODEX"),
            ("STATE_CLOSED", "STATE_CLOSED"),
            ("STATE_OPEN", "STATE_OPEN"),
            ("TEST_BASE_BRANCH", "TEST_BASE_BRANCH"),
            ("TEST_REPO_SLUG", "TEST_REPO_SLUG"),
            ("_FAKE_WT", "_FAKE_WT"),
            ("_TEST_SPEC", "_TEST_SPEC"),
        ),
    ),
    *export_group(
        "tests.workflow_stage_labels",
        (
            ("LABEL_DECOMPOSING", "LABEL_DECOMPOSING"),
            ("LABEL_DOCUMENTING", "LABEL_DOCUMENTING"),
            ("LABEL_FIXING", "LABEL_FIXING"),
            ("LABEL_IMPLEMENTING", "LABEL_IMPLEMENTING"),
            ("LABEL_IN_REVIEW", "LABEL_IN_REVIEW"),
            ("LABEL_QUESTION", "LABEL_QUESTION"),
            ("LABEL_VALIDATING", "LABEL_VALIDATING"),
        ),
    ),
    *export_group(
        "tests.workflow_state_values",
        (
            ("KEY_AWAITING_HUMAN", "KEY_AWAITING_HUMAN"),
            ("KEY_ISSUE_AGENT_RUNS", "KEY_ISSUE_AGENT_RUNS"),
            ("KEY_ISSUE_TOTAL_TOKENS", "KEY_ISSUE_TOTAL_TOKENS"),
            (
                "KEY_LAST_ACTION_COMMENT_ID",
                "KEY_LAST_ACTION_COMMENT_ID",
            ),
            ("KEY_PARENT_NUMBER", "KEY_PARENT_NUMBER"),
            ("KEY_PARK_REASON", "KEY_PARK_REASON"),
            ("ROLE_DEVELOPER", "ROLE_DEVELOPER"),
            ("ROLE_REVIEWER", "ROLE_REVIEWER"),
        ),
    ),
    *export_group(
        "tests.workflow_value_helpers",
        (
            ("_analytics_records", "_analytics_records"),
            ("_fake_worktree", "_fake_worktree"),
            ("_iso_hours_ago", "_iso_hours_ago"),
            ("_issue_branch", "_issue_branch"),
            ("_manifest", "_manifest"),
            ("_state_with_pr_number", "_state_with_pr_number"),
        ),
    ),
    *export_group(
        "tests.workflow_verdict_values",
        (
            ("REVIEW_APPROVED_MESSAGE", "REVIEW_APPROVED_MESSAGE"),
            (
                "REVIEW_CHANGES_REQUESTED_MESSAGE",
                "REVIEW_CHANGES_REQUESTED_MESSAGE",
            ),
            ("VERDICT_APPROVED", "VERDICT_APPROVED"),
            (
                "VERDICT_CHANGES_REQUESTED",
                "VERDICT_CHANGES_REQUESTED",
            ),
            ("VERDICT_UNKNOWN", "VERDICT_UNKNOWN"),
        ),
    ),
)
