# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Fresh-spawn implementing flow: clean tree -> PR, parks for dirty trees /
silent failures / pushed-failure, awaiting-human resume, and the
recovered-worktree shortcut that skips the dev agent."""

from __future__ import annotations

import unittest

from orchestrator import workflow

from tests import implementing_fresh_test_support
from tests.fakes import (
    FakeComment,
    FakeGitHubClient,
    FakeUser,
    make_issue,
)
from tests.implementing_fixing_test_cases import IssueScenario
from tests.workflow_helpers import (
    LABEL_IMPLEMENTING,
    _PatchedWorkflowMixin,
    _agent,
)

AWAITING_HUMAN = "awaiting_human"
RUN_AGENT = "run_agent"
LEGACY_SESSION = "sess-old"
ACTION_COMMENT_ID = 900
HUMAN_REPLY_ID = 1100
INTERRUPTED_RESUME_ISSUE = 70
INTERRUPTED_SPAWN_ISSUE = 71
DIRTY_FILE_COUNT = 15


def _seed_fresh_issue(label=LABEL_IMPLEMENTING):
    gh = FakeGitHubClient()
    issue = make_issue(1, label=label)
    gh.add_issue(issue)
    return gh, issue


class HandleImplementingFreshRunTest(unittest.TestCase, _PatchedWorkflowMixin):
    def test_clean_commits_open_pr_and_flip_label(self) -> None:
        # After PR open, hand off straight to `validating`; the docs pass only
        # runs as the final-docs handoff after the reviewer agent approves, not
        # as a pre-review hop. Implementing routes to the reviewer path and
        # never straight to `in_review`.
        scenario = IssueScenario(*_seed_fresh_issue())
        self._run_implementing(
            scenario.github,
            scenario.issue,
            run_agent=_agent(session_id="sess-1", last_message="implemented"),
            # First call: not a recovered worktree -> codex runs.
            # Second call: codex produced commits -> push path.
            has_new_commits=[False, True],
            dirty_files=(),
            push_branch=True,
        )

        implementing_fresh_test_support.assert_pr_routing(
            self,
            scenario,
        )
        implementing_fresh_test_support.assert_pr_state(
            self,
            scenario,
        )

    def test_dirty_commits_park_without_push(self) -> None:
        scenario = IssueScenario(*_seed_fresh_issue())
        dirty = [f"file_{file_index}.py" for file_index in range(DIRTY_FILE_COUNT)]
        mocks = self._run_implementing(
            scenario.github,
            scenario.issue,
            run_agent=_agent(last_message="commit done but more work pending"),
            has_new_commits=[False, True],
            dirty_files=dirty,
            push_branch=True,
        )

        mocks["_push_branch"].assert_not_called()
        self.assertEqual(scenario.github.opened_prs, [])
        self.assertTrue(scenario.github.pinned_data(1).get(AWAITING_HUMAN))
        last_comment = scenario.github.posted_comments[-1][1]
        self.assertIn("file_0.py", last_comment)
        self.assertIn("file_9.py", last_comment)
        self.assertNotIn("file_10.py", last_comment)
        self.assertIn("… (5 more)", last_comment)

    def test_no_commits_message_parks_as_question(self) -> None:
        gh, issue = _seed_fresh_issue()
        self._run_implementing(
            gh,
            issue,
            run_agent=_agent(last_message="What database should I use?"),
            has_new_commits=False,
        )

        self.assertEqual(gh.opened_prs, [])
        state = gh.pinned_data(1)
        self.assertTrue(state.get(AWAITING_HUMAN))
        last_comment = gh.posted_comments[-1][1]
        self.assertIn("> What database should I use?", last_comment)
        self.assertIn("agent needs your input", last_comment)
        # A real question with content is not a silent failure.
        self.assertIsNone(state.get("park_reason"))
        self.assertEqual(state.get("silent_park_count", 0), 0)

    def test_empty_run_parks_as_silent_failure(self) -> None:
        # Empty `last_message` AND no commits is the poisoned-resume shape
        # documented in #24: a session killed mid-stream (e.g. by a Claude
        # rate limit) consistently returns empty results on every resume.
        # The park must surface as a silent failure (distinct
        # `park_reason`, distinct HITL message) instead of impersonating a
        # real "agent has a content question" park.
        gh, issue = _seed_fresh_issue()
        self._run_implementing(
            gh,
            issue,
            run_agent=_agent(last_message=""),
            has_new_commits=False,
        )

        state = gh.pinned_data(1)
        self.assertTrue(state.get(AWAITING_HUMAN))
        self.assertEqual(state.get("park_reason"), "agent_silent")
        self.assertEqual(state.get("silent_park_count"), 1)
        last_comment = gh.posted_comments[-1][1]
        self.assertIn("agent produced no output", last_comment)
        self.assertIn("session-resume failure", last_comment)
        self.assertNotIn("agent needs your input", last_comment)
        # No quoted empty-message body either.
        self.assertNotIn("> (agent did not produce a final message)", last_comment)

    def test_silent_park_includes_stderr(self) -> None:
        # Same shape as the silent-failure park, but the agent left
        # something on stderr (e.g. a Cloudflare blob, an auth error).
        # The park comment must surface that tail and the exit code so
        # the operator can triage without reading ~/.codex/log/.
        scenario = IssueScenario(*_seed_fresh_issue())
        with self.assertLogs("orchestrator.workflow", level="WARNING") as logs:
            self._run_implementing(
                scenario.github,
                scenario.issue,
                run_agent=_agent(
                    last_message="",
                    stderr="401 Unauthorized: token expired",
                    exit_code=1,
                ),
                has_new_commits=False,
            )
            log_messages = [record.getMessage() for record in logs.records]

        last_comment = scenario.github.posted_comments[-1][1]
        self.assertIn("agent produced no output", last_comment)
        self.assertIn("_Agent stderr (last 1KB):_", last_comment)
        self.assertIn("401 Unauthorized", last_comment)
        self.assertIn("_Agent exit code:_ 1", last_comment)
        self.assertTrue(
            any("agent produced no output" in message and "exit_code=1" in message for message in log_messages)
        )

    def test_push_failure_parks_without_opening_pr(self) -> None:
        gh, issue = _seed_fresh_issue()
        mocks = self._run_implementing(
            gh,
            issue,
            run_agent=_agent(session_id="sess-1", last_message="done"),
            has_new_commits=[False, True],
            dirty_files=(),
            push_branch=False,
        )

        mocks["_push_branch"].assert_called_once()
        self.assertEqual(gh.opened_prs, [])
        self.assertTrue(gh.pinned_data(1).get(AWAITING_HUMAN))
        last_comment = gh.posted_comments[-1][1]
        self.assertIn("git push failed", last_comment)


class HandleImplementingAwaitingHumanTest(unittest.TestCase, _PatchedWorkflowMixin):
    def test_no_comments_return_without_state_write(self) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(2, label=LABEL_IMPLEMENTING)
        gh.add_issue(issue)
        # Pre-seed `user_content_hash` so the durability-fix branch in
        # `_detect_user_content_change` doesn't trigger an extra
        # baseline-seeding write; this test specifically verifies the
        # awaiting-human no-reply path produces zero state churn.
        gh.seed_state(
            2,
            awaiting_human=True,
            last_action_comment_id=ACTION_COMMENT_ID,
            codex_session_id=LEGACY_SESSION,
            user_content_hash=workflow._compute_user_content_hash(issue, set()),
        )
        before = gh.write_state_calls

        mocks = self._run_implementing(
            gh,
            issue,
            run_agent=_agent(),
        )

        mocks[RUN_AGENT].assert_not_called()
        self.assertEqual(gh.write_state_calls, before)
        # Pinned data unchanged.
        self.assertTrue(gh.pinned_data(2).get(AWAITING_HUMAN))
        self.assertEqual(gh.pinned_data(2).get("codex_session_id"), LEGACY_SESSION)

    def test_new_comments_resume_and_clear_park(self) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(2, label=LABEL_IMPLEMENTING)
        reply = FakeComment(
            id=HUMAN_REPLY_ID,
            body="please use sqlite",
            user=FakeUser("alice"),
        )
        issue.comments.append(reply)
        gh.add_issue(issue)
        gh.seed_state(
            2,
            awaiting_human=True,
            last_action_comment_id=ACTION_COMMENT_ID,
            codex_session_id=LEGACY_SESSION,
            branch="orchestrator/geserdugarov__agent-orchestrator/issue-2",
        )

        mocks = self._run_implementing(
            gh,
            issue,
            run_agent=_agent(session_id=LEGACY_SESSION, last_message="ok"),
            # awaiting_human path skips the recovered-worktree probe; only
            # the post-codex commit check runs.
            has_new_commits=[True],
            dirty_files=(),
            push_branch=True,
        )

        implementing_fresh_test_support.assert_human_reply_resume(
            self,
            gh,
            mocks,
            RUN_AGENT,
            LEGACY_SESSION,
        )


class HandleImplementingInterruptedTest(unittest.TestCase, _PatchedWorkflowMixin):
    """A dev run the shutdown sweep killed mid-flight (`AgentResult.interrupted`)
    must be ignored: the handler returns quietly WITHOUT writing pinned state,
    so durable GitHub state stays retryable by the next process. It must not
    park, post a HITL question, consume `awaiting_human`, advance the
    action watermark, or open a PR off a partial result."""

    def test_awaiting_human_resume_leaves_state(
        self,
    ) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(INTERRUPTED_RESUME_ISSUE, label=LABEL_IMPLEMENTING)
        reply = FakeComment(
            id=HUMAN_REPLY_ID,
            body="please use sqlite",
            user=FakeUser("alice"),
        )
        issue.comments.append(reply)
        gh.add_issue(issue)
        gh.seed_state(
            INTERRUPTED_RESUME_ISSUE,
            awaiting_human=True,
            last_action_comment_id=ACTION_COMMENT_ID,
            codex_session_id=LEGACY_SESSION,
            branch=f"orchestrator/geserdugarov__agent-orchestrator/issue-{INTERRUPTED_RESUME_ISSUE}",
            user_content_hash=workflow._compute_user_content_hash(issue, set()),
        )
        before_writes = gh.write_state_calls

        self._mocks = self._run_implementing(
            gh,
            issue,
            run_agent=_agent(session_id=LEGACY_SESSION, interrupted=True),
        )

        # The resume DID spawn -- the interruption is observed only after
        # the agent returns.
        self._mocks[RUN_AGENT].assert_called_once()
        implementing_fresh_test_support.assert_interrupted_resume_state(
            self,
            gh,
            before_writes,
            INTERRUPTED_RESUME_ISSUE,
            ACTION_COMMENT_ID,
        )

    def test_interrupted_spawn_keeps_session_pr_clear(
        self,
    ) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(INTERRUPTED_SPAWN_ISSUE, label=LABEL_IMPLEMENTING)
        gh.add_issue(issue)
        # Seed the content hash so the first-encounter drift baseline write
        # doesn't fire -- this test asserts ZERO state writes.
        gh.seed_state(
            INTERRUPTED_SPAWN_ISSUE,
            user_content_hash=workflow._compute_user_content_hash(issue, set()),
        )
        before_writes = gh.write_state_calls

        mocks = self._run_implementing(
            gh,
            issue,
            run_agent=_agent(session_id="sess-new", interrupted=True),
            # First probe: not a recovered worktree -> the dev runs and is
            # then seen to be interrupted; the post-agent commit check must
            # never be reached.
            has_new_commits=[False],
        )

        mocks[RUN_AGENT].assert_called_once()
        self.assertEqual(gh.write_state_calls, before_writes)
        self.assertEqual(gh.opened_prs, [])
        self.assertEqual(gh.label_history, [])
        state = gh.pinned_data(INTERRUPTED_SPAWN_ISSUE)
        # The interrupted spawn's session id is NOT persisted -- the next
        # process re-spawns fresh rather than resuming a half-built session.
        self.assertNotIn("dev_session_id", state)


class HandleImplementingRecoveredWorktreeTest(unittest.TestCase, _PatchedWorkflowMixin):
    def test_recovered_tree_skips_agent_and_pushes(self) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(3, label=LABEL_IMPLEMENTING)
        gh.add_issue(issue)
        gh.seed_state(3, codex_session_id="sess-prev")

        mocks = self._run_implementing(
            gh,
            issue,
            run_agent=_agent(),
            has_new_commits=True,
            dirty_files=(),
            push_branch=True,
        )

        mocks[RUN_AGENT].assert_not_called()
        mocks["_push_branch"].assert_called_once()
        self.assertEqual(len(gh.opened_prs), 1)
        # Prior session id retained.
        self.assertEqual(gh.pinned_data(3).get("codex_session_id"), "sess-prev")
