# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Focused mock groups for the shared workflow runner."""
from __future__ import annotations

from unittest.mock import MagicMock

from orchestrator.worktrees import VerifyResult

from tests.workflow_patch_models import (
    _WorkflowRunContext,
    _as_mock,
    _default_infer_subject_prefix,
)
from tests.workflow_repo_values import _FAKE_WT


def _execution_mocks(context: _WorkflowRunContext) -> dict[str, object]:
    new_commits = MagicMock()
    commit_sequence = context.has_new_commits
    if isinstance(commit_sequence, (list, tuple)):
        new_commits.side_effect = list(commit_sequence)
    else:
        new_commits.return_value = bool(commit_sequence)
    return {
        "run_agent": _as_mock(context.run_agent),
        "_has_new_commits": new_commits,
        "_worktree_dirty_files": MagicMock(
            return_value=list(context.dirty_files),
        ),
    }


def _worktree_mocks(context: _WorkflowRunContext) -> dict[str, object]:
    return {
        "_ensure_worktree": MagicMock(return_value=_FAKE_WT),
        "_ensure_pr_worktree": MagicMock(return_value=_FAKE_WT),
        "_ensure_decompose_worktree": MagicMock(return_value=_FAKE_WT),
        "_decompose_worktree_path": MagicMock(return_value=_FAKE_WT),
    }


def _cleanup_mocks(context: _WorkflowRunContext) -> dict[str, object]:
    return {
        "_cleanup_decompose_worktree": MagicMock(),
        "_cleanup_question_worktree": MagicMock(),
        "_cleanup_terminal_branch": MagicMock(),
        "_branch_has_unpushed_commits": MagicMock(return_value=None),
    }


def _publication_mocks(context: _WorkflowRunContext) -> dict[str, object]:
    if context.fallback_prefix is None:
        prefix_mock = MagicMock(
            side_effect=_default_infer_subject_prefix,
        )
    else:
        prefix_mock = MagicMock(return_value=context.fallback_prefix)
    return {
        "_push_branch": MagicMock(return_value=bool(context.push_branch)),
        "_head_sha": MagicMock(side_effect=list(context.head_shas)),
        "_first_commit_subject": MagicMock(
            return_value=context.first_commit_subject,
        ),
        "_infer_subject_prefix": prefix_mock,
    }


def _validation_mocks(context: _WorkflowRunContext) -> dict[str, object]:
    verify_result = context.verify_result
    if verify_result is None:
        verify_result = VerifyResult(status="ok")
    return {
        "_squash_and_force_push": MagicMock(
            return_value=tuple(context.squash_result),
        ),
        "_run_verify_commands": MagicMock(return_value=verify_result),
        "_rebase_in_progress": MagicMock(
            return_value=bool(context.rebase_in_progress),
        ),
    }


def _conflict_mocks(context: _WorkflowRunContext) -> dict[str, object]:
    fetch_result = context.authed_fetch_result
    if fetch_result is None:
        fetch_result = MagicMock(returncode=0, stdout="", stderr="")
    return {
        "_authed_fetch": MagicMock(return_value=fetch_result),
        "_branch_ahead_behind": MagicMock(
            return_value=tuple(context.branch_ahead_behind),
        ),
    }


_MOCK_BUILDERS = (
    _execution_mocks,
    _worktree_mocks,
    _cleanup_mocks,
    _publication_mocks,
    _validation_mocks,
    _conflict_mocks,
)


def _build_workflow_mocks(
    context: _WorkflowRunContext,
) -> dict[str, object]:
    workflow_mocks: dict[str, object] = {}
    for build_group in _MOCK_BUILDERS:
        workflow_mocks.update(build_group(context))
    return workflow_mocks
