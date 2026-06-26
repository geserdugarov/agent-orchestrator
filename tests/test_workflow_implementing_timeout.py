# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Implementing-stage agent-timeout disposition and recovery.

A timed-out implementer can still have committed clean work (or a descendant
the timeout cleanup raced finishes the commit just after). The handler must
not strand that commit behind `awaiting_human`: a clean HEAD advance pushes
and opens the PR, a dirty advance parks for inspection, and a no-commit
timeout parks tagged `agent_timeout` + `pre_implement_sha` so the next tick
can publish a late-landing commit without a human comment."""
from __future__ import annotations

import os
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("ORCHESTRATOR_SKIP_DOTENV", "1")

from orchestrator import workflow

from tests.fakes import (
    FakeGitHubClient,
    make_issue,
)
from tests.workflow_helpers import (
    _PatchedWorkflowMixin,
    _TEST_SPEC,
    _agent,
)


class HandleImplementingTimeoutDispositionTest(
    unittest.TestCase, _PatchedWorkflowMixin
):
    """Inline disposition when the fresh implementer spawn times out."""

    def _seeded(self):
        gh = FakeGitHubClient()
        issue = make_issue(1, label="implementing")
        gh.add_issue(issue)
        return gh, issue

    def test_timeout_no_commit_parks_with_agent_timeout_reason(self) -> None:
        # HEAD did not advance past the pre-agent SHA: the timeout produced no
        # commit. Park awaiting human, no push, no PR -- but tag the park
        # `agent_timeout` and persist `pre_implement_sha` for next-tick
        # recovery (the old path left `park_reason=None`).
        gh, issue = self._seeded()
        mocks = self._run(
            lambda: workflow._handle_implementing(gh, _TEST_SPEC, issue),
            run_agent=_agent(timed_out=True),
            # before_sha then after_sha: identical -> no new commit.
            head_shas=("sha-pre", "sha-pre"),
        )

        mocks["_push_branch"].assert_not_called()
        self.assertEqual(gh.opened_prs, [])
        data = gh.pinned_data(1)
        self.assertTrue(data.get("awaiting_human"))
        self.assertEqual(data.get("park_reason"), "agent_timeout")
        self.assertEqual(data.get("pre_implement_sha"), "sha-pre")
        last_comment = gh.posted_comments[-1][1]
        self.assertIn("agent timed out", last_comment)
        self.assertNotIn((1, "validating"), gh.label_history)

    def test_timeout_clean_commit_pushes_opens_pr_and_flips_label(self) -> None:
        # HEAD advanced and the tree is clean: the agent committed clean work
        # before the timeout killed it. Publish exactly like a normal
        # completion -- push, open PR, route to validating.
        gh, issue = self._seeded()
        self._run(
            lambda: workflow._handle_implementing(gh, _TEST_SPEC, issue),
            run_agent=_agent(
                session_id="sess-1", timed_out=True,
                last_message="partial trace before the kill",
            ),
            head_shas=("sha-pre", "sha-post"),  # HEAD advanced.
            dirty_files=(),
            push_branch=True,
        )

        self.assertEqual(len(gh.opened_prs), 1)
        opened = gh.opened_prs[0]
        self.assertTrue(any(
            f":sparkles: PR opened: #{opened.number}" in body
            for _, body in gh.posted_comments
        ))
        self.assertIn((1, "validating"), gh.label_history)
        data = gh.pinned_data(1)
        self.assertEqual(data["pr_number"], opened.number)
        # A timeout-publish must not strand the issue awaiting a human, and
        # the timeout watermark is spent once the commit ships.
        self.assertFalse(data.get("awaiting_human"))
        self.assertIsNone(data.get("pre_implement_sha"))

    def test_timeout_dirty_commit_parks_without_pushing(self) -> None:
        # HEAD advanced but the tree carries uncommitted edits. Pushing would
        # publish an incomplete branch, so park for inspection instead.
        gh, issue = self._seeded()
        mocks = self._run(
            lambda: workflow._handle_implementing(gh, _TEST_SPEC, issue),
            run_agent=_agent(timed_out=True, last_message="committed then died"),
            head_shas=("sha-pre", "sha-post"),  # HEAD advanced.
            dirty_files=["leftover.py"],
        )

        mocks["_push_branch"].assert_not_called()
        self.assertEqual(gh.opened_prs, [])
        data = gh.pinned_data(1)
        self.assertTrue(data.get("awaiting_human"))
        last_comment = gh.posted_comments[-1][1]
        self.assertIn("leftover.py", last_comment)
        self.assertNotIn((1, "validating"), gh.label_history)


class HandleImplementingTimeoutRecoveryTest(
    unittest.TestCase, _PatchedWorkflowMixin
):
    """Next-tick recovery of a commit stranded by an `agent_timeout` park."""

    def _parked(self, **overrides):
        gh = FakeGitHubClient()
        issue = make_issue(4, label="implementing")
        gh.add_issue(issue)
        state = dict(
            awaiting_human=True,
            park_reason="agent_timeout",
            pre_implement_sha="sha-pre",
            last_action_comment_id=900,
            dev_agent="codex",
            dev_session_id="sess-x",
            branch="orchestrator/geserdugarov__agent-orchestrator/issue-4",
            user_content_hash=workflow._compute_user_content_hash(issue, set()),
        )
        state.update(overrides)
        gh.seed_state(4, **state)
        return gh, issue

    def test_parked_timeout_recovers_clean_commit_without_human(self) -> None:
        # A descendant finished a clean commit after the timeout was recorded
        # (the #77 shape). With no human comment, the next tick must publish
        # the recovered commit and clear the park rather than wait forever.
        gh, issue = self._parked()
        with patch.object(workflow, "_worktree_path", return_value=Path("/tmp")):
            mocks = self._run(
                lambda: workflow._handle_implementing(gh, _TEST_SPEC, issue),
                run_agent=_agent(),
                head_shas=("sha-post",),  # HEAD advanced past pre_implement_sha.
                dirty_files=(),
                push_branch=True,
            )

        # No agent spawned -- the commit was already on the branch.
        mocks["run_agent"].assert_not_called()
        mocks["_push_branch"].assert_called_once()
        self.assertEqual(len(gh.opened_prs), 1)
        self.assertIn((4, "validating"), gh.label_history)
        data = gh.pinned_data(4)
        self.assertFalse(data.get("awaiting_human"))
        self.assertIsNone(data.get("park_reason"))
        self.assertIsNone(data.get("pre_implement_sha"))

    def test_parked_timeout_no_commit_stays_parked_silently(self) -> None:
        # HEAD is unchanged from the pre-timeout SHA: nothing recoverable.
        # Stay parked with zero churn -- no push, no PR, no relabel, and no
        # second park comment.
        gh, issue = self._parked()
        before_writes = gh.write_state_calls
        before_comments = len(gh.posted_comments)
        with patch.object(workflow, "_worktree_path", return_value=Path("/tmp")):
            mocks = self._run(
                lambda: workflow._handle_implementing(gh, _TEST_SPEC, issue),
                run_agent=_agent(),
                head_shas=("sha-pre",),  # HEAD == pre_implement_sha: no commit.
                dirty_files=(),
            )

        mocks["run_agent"].assert_not_called()
        mocks["_push_branch"].assert_not_called()
        self.assertEqual(gh.opened_prs, [])
        self.assertEqual(gh.label_history, [])
        self.assertEqual(gh.write_state_calls, before_writes)
        self.assertEqual(len(gh.posted_comments), before_comments)
        data = gh.pinned_data(4)
        self.assertTrue(data.get("awaiting_human"))
        self.assertEqual(data.get("park_reason"), "agent_timeout")

    def test_parked_timeout_dirty_tree_stays_parked(self) -> None:
        # HEAD advanced but a descendant left uncommitted edits -- publishing
        # would ship an incomplete branch, so stay parked for inspection.
        gh, issue = self._parked()
        with patch.object(workflow, "_worktree_path", return_value=Path("/tmp")):
            mocks = self._run(
                lambda: workflow._handle_implementing(gh, _TEST_SPEC, issue),
                run_agent=_agent(),
                dirty_files=["half-written.py"],
            )

        mocks["run_agent"].assert_not_called()
        mocks["_push_branch"].assert_not_called()
        self.assertEqual(gh.opened_prs, [])
        data = gh.pinned_data(4)
        self.assertTrue(data.get("awaiting_human"))
        self.assertEqual(data.get("park_reason"), "agent_timeout")

    def test_parked_timeout_human_reply_resumes_dev(self) -> None:
        # When the human DID reply, their comment is the resume signal: the
        # dev session resumes on it instead of the silent recovery firing.
        from tests.fakes import FakeComment, FakeUser

        gh = FakeGitHubClient()
        issue = make_issue(4, label="implementing")
        issue.comments.append(
            FakeComment(id=1500, body="please continue", user=FakeUser("alice"))
        )
        gh.add_issue(issue)
        # Seed the content hash AFTER the comment so drift detection (which
        # hashes human comments too) does not divert the resume into the
        # body-change path.
        gh.seed_state(
            4,
            awaiting_human=True,
            park_reason="agent_timeout",
            pre_implement_sha="sha-pre",
            last_action_comment_id=900,
            dev_agent="codex",
            dev_session_id="sess-x",
            branch="orchestrator/geserdugarov__agent-orchestrator/issue-4",
            user_content_hash=workflow._compute_user_content_hash(issue, set()),
        )
        with patch.object(workflow, "_worktree_path", return_value=Path("/tmp")):
            mocks = self._run(
                lambda: workflow._handle_implementing(gh, _TEST_SPEC, issue),
                run_agent=_agent(session_id="sess-x", last_message="done"),
                head_shas=("sha-pre",),  # before_sha snapshot for the resume.
                has_new_commits=[True],
                dirty_files=(),
                push_branch=True,
            )

        # The dev resumed on the human comment rather than a silent recovery.
        mocks["run_agent"].assert_called_once()
        followup = mocks["run_agent"].call_args.args[1]
        self.assertIn("please continue", followup)


if __name__ == "__main__":
    unittest.main()
