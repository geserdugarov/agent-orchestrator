# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Shared fixtures and helpers for workflow stage tests.

The workflow-stage test files patch the same worktree / push / squash helpers
on every run and build `AgentResult` / `MagicMock` plumbing the same way. They
all import the constants and the `_PatchedWorkflowMixin` from here so the
patch surface stays in one place when a new helper is plumbed through.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
from unittest.mock import MagicMock, patch

from orchestrator import config, workflow
from orchestrator.agents import AgentResult


def _iso_hours_ago(hours: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat(
        timespec="seconds"
    )


def _manifest(payload: str) -> str:
    return f"```orchestrator-manifest\n{payload}\n```"


_FAKE_WT = Path("/tmp/orchestrator-test-wt-doesnt-matter")
# Tests don't shell out (the worktree/git helpers are mocked), so the values
# only need to be plausible -- the slug/base reach `_build_review_prompt`,
# `_push_branch`, and the `find_open_pr` / `open_pr` call sites and are
# inspected by some assertions; nothing else cares.
_TEST_SPEC = config.RepoSpec(
    slug="geserdugarov/agent-orchestrator",
    target_root=Path("/tmp/orchestrator-test-target-root"),
    base_branch="main",
)


def _agent(
    *,
    session_id: str = "sess-1",
    last_message: str = "",
    timed_out: bool = False,
    stderr: str = "",
    exit_code: Optional[int] = None,
) -> AgentResult:
    return AgentResult(
        session_id=session_id,
        last_message=last_message,
        exit_code=exit_code if exit_code is not None else (-1 if timed_out else 0),
        timed_out=timed_out,
        stdout="",
        stderr=stderr,
    )


def _as_mock(value_or_seq):
    if callable(value_or_seq):
        return value_or_seq
    if isinstance(value_or_seq, (list, tuple)):
        m = MagicMock()
        m.side_effect = list(value_or_seq)
        return m
    m = MagicMock()
    m.return_value = value_or_seq
    return m


class _PatchedWorkflowMixin:
    """Helper that wires standard patches around a single test body."""

    def _run(
        self,
        callable_,
        *,
        run_agent,
        has_new_commits=False,
        dirty_files=(),
        push_branch=True,
        head_shas=("",),
        first_commit_subject="",
        squash_result=(True, None, 0, None),
        branch_ahead_behind=(0, 0),
        rebase_in_progress=False,
        verify_result=None,
        authed_fetch_result=None,
    ):
        rc_mock = _as_mock(run_agent)
        hnc_seq = has_new_commits if isinstance(has_new_commits, (list, tuple)) else None
        hnc_mock = MagicMock()
        if hnc_seq is not None:
            hnc_mock.side_effect = list(hnc_seq)
        else:
            hnc_mock.return_value = bool(has_new_commits)

        df_mock = MagicMock(return_value=list(dirty_files))
        push_mock = MagicMock(return_value=bool(push_branch))
        head_mock = MagicMock(side_effect=list(head_shas))
        wt_mock = MagicMock(return_value=_FAKE_WT)
        # `_ensure_pr_worktree` is the resolving_conflict-specific helper
        # that restores from `origin/<branch>`; mock it on the same fake
        # path so resolving_conflict tests don't shell out either.
        pr_wt_mock = MagicMock(return_value=_FAKE_WT)
        # `_authed_fetch` runs an actual subprocess in production; mock
        # it to a successful CompletedProcess so resolving_conflict tests
        # don't need a real askpass / token / network. Tests that want
        # to exercise the fetch-failure park branch pass an explicit
        # `authed_fetch_result` (e.g. `MagicMock(returncode=1,
        # stderr="...")`) to drive that path.
        authed_fetch_ok = MagicMock(
            return_value=(
                authed_fetch_result
                if authed_fetch_result is not None
                else MagicMock(returncode=0, stdout="", stderr="")
            )
        )
        # `_branch_ahead_behind` runs `git rev-list` in the worktree;
        # default to (0, 0) ("in sync") so existing tests don't have to
        # opt into the SHA-alignment recovery path. Tests that DO want
        # to exercise the recovery / stale / diverged branches pass a
        # different tuple via the `branch_ahead_behind` kwarg.
        ahead_behind_mock = MagicMock(return_value=tuple(branch_ahead_behind))
        # `_branch_has_unpushed_commits` shells out to `git rev-list`
        # against the parent clone; default to False ("local branch is
        # clean or absent") so existing tests don't trip the
        # question-stage-park branch check in `_handle_implementing`.
        # The question-stage relabel tests override this mock to True
        # to assert the unsafe-branch refusal.
        branch_unpushed_mock = MagicMock(return_value=False)
        # Decomposer worktree helpers run real `git` calls in production.
        # Mock them with the same _FAKE_WT so `_handle_decomposing` tests
        # don't shell out (and the cleanup helper is a no-op).
        decompose_wt_mock = MagicMock(return_value=_FAKE_WT)
        decompose_path_mock = MagicMock(return_value=_FAKE_WT)
        cleanup_decompose_mock = MagicMock()
        # `_cleanup_question_worktree` runs at every safe exit of
        # `_handle_question` to tear down the read-only worktree.
        # Production calls `git worktree remove` + `git branch -D`;
        # mocked here so tests don't shell out.
        cleanup_question_mock = MagicMock()
        # `_on_commits` reads the worktree's first commit subject to derive
        # the PR title; mock it so tests don't shell out to git.
        first_subject_mock = MagicMock(return_value=first_commit_subject)
        cleanup_terminal_mock = MagicMock()
        # Squash helper would otherwise shell out to `git merge-base` etc.
        # against `_FAKE_WT`. Default: success-no-op, so tests not exercising
        # the squash path see no agent_approved_sha override.
        squash_mock = MagicMock(return_value=tuple(squash_result))
        rebase_in_progress_mock = MagicMock(return_value=bool(rebase_in_progress))
        # Verify-commands runner shells out to the operator's configured
        # commands. Default: "ok" so tests not exercising the verify gate
        # see no behavior change. The default-empty `VERIFY_COMMANDS` also
        # short-circuits the helper to ok before it spawns anything, but
        # the mock is in place so a test that sets VERIFY_COMMANDS does
        # not accidentally shell out.
        from orchestrator.worktrees import VerifyResult
        verify_mock = MagicMock(
            return_value=verify_result if verify_result is not None
            else VerifyResult(status="ok")
        )

        with patch.object(workflow, "run_agent", rc_mock), \
             patch.object(workflow, "_ensure_worktree", wt_mock), \
             patch.object(workflow, "_ensure_pr_worktree", pr_wt_mock), \
             patch.object(workflow, "_ensure_decompose_worktree", decompose_wt_mock), \
             patch.object(workflow, "_decompose_worktree_path", decompose_path_mock), \
             patch.object(workflow, "_cleanup_decompose_worktree", cleanup_decompose_mock), \
             patch.object(workflow, "_cleanup_question_worktree", cleanup_question_mock), \
             patch.object(workflow, "_cleanup_terminal_branch", cleanup_terminal_mock), \
             patch.object(workflow, "_has_new_commits", hnc_mock), \
             patch.object(workflow, "_worktree_dirty_files", df_mock), \
             patch.object(workflow, "_push_branch", push_mock), \
             patch.object(workflow, "_head_sha", head_mock), \
             patch.object(workflow, "_first_commit_subject", first_subject_mock), \
             patch.object(workflow, "_squash_and_force_push", squash_mock), \
             patch.object(workflow, "_run_verify_commands", verify_mock), \
             patch.object(workflow, "_authed_fetch", authed_fetch_ok), \
             patch.object(workflow, "_branch_ahead_behind", ahead_behind_mock), \
             patch.object(workflow, "_branch_has_unpushed_commits", branch_unpushed_mock), \
             patch.object(workflow, "_rebase_in_progress", rebase_in_progress_mock):
            callable_()

        return {
            "run_agent": rc_mock,
            "_ensure_worktree": wt_mock,
            "_ensure_pr_worktree": pr_wt_mock,
            "_ensure_decompose_worktree": decompose_wt_mock,
            "_decompose_worktree_path": decompose_path_mock,
            "_cleanup_decompose_worktree": cleanup_decompose_mock,
            "_cleanup_question_worktree": cleanup_question_mock,
            "_cleanup_terminal_branch": cleanup_terminal_mock,
            "_has_new_commits": hnc_mock,
            "_worktree_dirty_files": df_mock,
            "_push_branch": push_mock,
            "_head_sha": head_mock,
            "_first_commit_subject": first_subject_mock,
            "_squash_and_force_push": squash_mock,
            "_run_verify_commands": verify_mock,
            "_authed_fetch": authed_fetch_ok,
            "_branch_ahead_behind": ahead_behind_mock,
            "_branch_has_unpushed_commits": branch_unpushed_mock,
            "_rebase_in_progress": rebase_in_progress_mock,
        }
