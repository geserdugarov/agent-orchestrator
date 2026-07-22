# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Immutable lazy-export inventory for :mod:`orchestrator.stages.question`."""

from __future__ import annotations

from orchestrator._compat_exports import export_group

EXPORTS = (
    *export_group(
        "contextlib",
        (("contextlib", None),),
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
        "orchestrator.stages._question_handler",
        (
            ("_cleanup_question_run", "_cleanup_question_run"),
            ("_handle_question", "_handle_question"),
            ("_process_question_run", "_process_question_run"),
            ("_question_run_cleanup", "_question_run_cleanup"),
        ),
    ),
    *export_group(
        "orchestrator.stages._question_outcomes",
        (
            ("_assess_question_outcome", "_assess_question_outcome"),
            ("_assess_question_worktree", "_assess_question_worktree"),
            ("_park_answered_question", "_park_answered_question"),
            ("_park_dirty_question", "_park_dirty_question"),
            ("_park_silent_question", "_park_silent_question"),
            ("_route_question_outcome", "_route_question_outcome"),
        ),
    ),
    *export_group(
        "orchestrator.stages._question_run",
        (
            ("_finalize_closed_question", "_finalize_closed_question"),
            ("_park_question", "_park_question"),
            ("_resume_question_on_human_reply", "_resume_question_on_human_reply"),
            ("_select_question_run", "_select_question_run"),
            ("_spawn_fresh_question", "_spawn_fresh_question"),
        ),
    ),
    *export_group(
        "orchestrator.stages._question_session",
        (
            ("_QuestionOutcome", "_QuestionOutcome"),
            ("_QuestionRun", "_QuestionRun"),
            ("_QuestionSession", "_QuestionSession"),
            ("_build_question_resume_prompt", "_build_question_resume_prompt"),
            ("_consume_new_human_replies", "_consume_new_human_replies"),
            ("_execute_question_prompt", "_execute_question_prompt"),
            ("_read_question_session", "_read_question_session"),
        ),
    ),
    *export_group(
        "orchestrator.stages._question_state",
        (
            ("_QUESTION_AGENT_KEY", "_QUESTION_AGENT_KEY"),
            ("_QUESTION_ANSWER", "_QUESTION_ANSWER"),
            ("_QUESTION_COMMITS", "_QUESTION_COMMITS"),
            ("_QUESTION_DIRTY", "_QUESTION_DIRTY"),
            ("_QUESTION_SESSION_KEY", "_QUESTION_SESSION_KEY"),
            ("_QUESTION_SILENT", "_QUESTION_SILENT"),
            ("_QUESTION_STAGE", "_QUESTION_STAGE"),
            ("_QUESTION_TIMEOUT", "_QUESTION_TIMEOUT"),
            ("_UNSAFE_QUESTION_PARKS", "_UNSAFE_QUESTION_PARKS"),
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
)
EXPORTED_NAMES = None
