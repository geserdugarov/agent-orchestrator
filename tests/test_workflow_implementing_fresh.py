# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Fresh-spawn implementing flow: clean tree -> PR, parks for dirty trees /
silent failures / pushed-failure, awaiting-human resume, and the
recovered-worktree shortcut that skips the dev agent."""
from __future__ import annotations

import os
import unittest

os.environ.setdefault("ORCHESTRATOR_SKIP_DOTENV", "1")

from orchestrator import config, workflow

from tests.fakes import (
    FakeComment,
    FakeGitHubClient,
    FakeUser,
    make_issue,
)
from tests.workflow_helpers import (
    _PatchedWorkflowMixin,
    _TEST_SPEC,
    _agent,
)


class HandleImplementingFreshRunTest(unittest.TestCase, _PatchedWorkflowMixin):
    def _seeded(self, label="implementing"):
        gh = FakeGitHubClient()
        issue = make_issue(1, label=label)
        gh.add_issue(issue)
        # No prior pinned state; simulate just-after-pickup.
        return gh, issue

    def test_commits_clean_tree_opens_pr_and_flips_label(self) -> None:
        gh, issue = self._seeded()
        self._run(
            lambda: workflow._handle_implementing(gh, _TEST_SPEC, issue),
            run_agent=_agent(session_id="sess-1", last_message="implemented"),
            # First call: not a recovered worktree -> codex runs.
            # Second call: codex produced commits -> push path.
            has_new_commits=[False, True],
            dirty_files=(),
            push_branch=True,
        )

        self.assertEqual(len(gh.opened_prs), 1)
        opened = gh.opened_prs[0]
        self.assertTrue(any(
            f":sparkles: PR opened: #{opened.number}" in body
            for _, body in gh.posted_comments
        ))
        # After PR open, hand off straight to `validating`. The docs
        # pass only runs as the final-docs handoff after the reviewer
        # agent approves -- not as a pre-review hop.
        self.assertIn((1, "validating"), gh.label_history)
        self.assertNotIn((1, "documenting"), gh.label_history)
        data = gh.pinned_data(1)
        self.assertEqual(data["pr_number"], opened.number)
        self.assertEqual(data["branch"], "orchestrator/geserdugarov__agent-orchestrator/issue-1")
        # First fresh dev spawn writes the new keys; the legacy field is
        # deliberately not migrated.
        self.assertEqual(data["dev_agent"], config.DEV_AGENT)
        self.assertEqual(data["dev_session_id"], "sess-1")
        self.assertNotIn("codex_session_id", data)
        self.assertEqual(data["review_round"], 0)

    def test_commits_with_dirty_tree_parks_without_pushing(self) -> None:
        gh, issue = self._seeded()
        dirty = [f"file_{i}.py" for i in range(15)]
        mocks = self._run(
            lambda: workflow._handle_implementing(gh, _TEST_SPEC, issue),
            run_agent=_agent(last_message="commit done but more work pending"),
            has_new_commits=[False, True],
            dirty_files=dirty,
            push_branch=True,
        )

        mocks["_push_branch"].assert_not_called()
        self.assertEqual(gh.opened_prs, [])
        self.assertTrue(gh.pinned_data(1).get("awaiting_human"))
        last_comment = gh.posted_comments[-1][1]
        self.assertIn("file_0.py", last_comment)
        self.assertIn("file_9.py", last_comment)
        self.assertNotIn("file_10.py", last_comment)
        self.assertIn("… (5 more)", last_comment)

    def test_no_commits_with_message_parks_as_question(self) -> None:
        gh, issue = self._seeded()
        self._run(
            lambda: workflow._handle_implementing(gh, _TEST_SPEC, issue),
            run_agent=_agent(last_message="What database should I use?"),
            has_new_commits=False,
        )

        self.assertEqual(gh.opened_prs, [])
        data = gh.pinned_data(1)
        self.assertTrue(data.get("awaiting_human"))
        last_comment = gh.posted_comments[-1][1]
        self.assertIn("> What database should I use?", last_comment)
        self.assertIn("agent needs your input", last_comment)
        # A real question with content is not a silent failure.
        self.assertIsNone(data.get("park_reason"))
        self.assertEqual(data.get("silent_park_count", 0), 0)

    def test_no_commits_no_message_parks_as_silent_failure(self) -> None:
        # Empty `last_message` AND no commits is the poisoned-resume shape
        # documented in #24: a session killed mid-stream (e.g. by a Claude
        # rate limit) consistently returns empty results on every resume.
        # The park must surface as a silent failure (distinct
        # `park_reason`, distinct HITL message) instead of impersonating a
        # real "agent has a content question" park.
        gh, issue = self._seeded()
        self._run(
            lambda: workflow._handle_implementing(gh, _TEST_SPEC, issue),
            run_agent=_agent(last_message=""),
            has_new_commits=False,
        )

        data = gh.pinned_data(1)
        self.assertTrue(data.get("awaiting_human"))
        self.assertEqual(data.get("park_reason"), "agent_silent")
        self.assertEqual(data.get("silent_park_count"), 1)
        last_comment = gh.posted_comments[-1][1]
        self.assertIn("agent produced no output", last_comment)
        self.assertIn("session-resume failure", last_comment)
        self.assertNotIn("agent needs your input", last_comment)
        # No quoted empty-message body either.
        self.assertNotIn("> (agent did not produce a final message)", last_comment)

    def test_silent_failure_park_includes_stderr_diagnostics(self) -> None:
        # Same shape as the silent-failure park, but the agent left
        # something on stderr (e.g. a Cloudflare blob, an auth error).
        # The park comment must surface that tail and the exit code so
        # the operator can triage without reading ~/.codex/log/.
        gh, issue = self._seeded()
        with self.assertLogs("orchestrator.workflow", level="WARNING") as logs:
            self._run(
                lambda: workflow._handle_implementing(gh, _TEST_SPEC, issue),
                run_agent=_agent(
                    last_message="",
                    stderr="401 Unauthorized: token expired",
                    exit_code=1,
                ),
                has_new_commits=False,
            )

        last_comment = gh.posted_comments[-1][1]
        self.assertIn("agent produced no output", last_comment)
        self.assertIn("_Agent stderr (last 1KB):_", last_comment)
        self.assertIn("401 Unauthorized", last_comment)
        self.assertIn("_Agent exit code:_ 1", last_comment)
        self.assertTrue(any(
            "agent produced no output" in r.getMessage()
            and "exit_code=1" in r.getMessage()
            for r in logs.records
        ))

    def test_push_failure_parks_without_opening_pr(self) -> None:
        gh, issue = self._seeded()
        mocks = self._run(
            lambda: workflow._handle_implementing(gh, _TEST_SPEC, issue),
            run_agent=_agent(session_id="sess-1", last_message="done"),
            has_new_commits=[False, True],
            dirty_files=(),
            push_branch=False,
        )

        mocks["_push_branch"].assert_called_once()
        self.assertEqual(gh.opened_prs, [])
        self.assertTrue(gh.pinned_data(1).get("awaiting_human"))
        last_comment = gh.posted_comments[-1][1]
        self.assertIn("git push failed", last_comment)


class HandleImplementingAwaitingHumanTest(unittest.TestCase, _PatchedWorkflowMixin):
    def test_no_new_comments_returns_without_writing_state(self) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(2, label="implementing")
        gh.add_issue(issue)
        # Pre-seed `user_content_hash` so the durability-fix branch in
        # `_detect_user_content_change` doesn't trigger an extra
        # baseline-seeding write; this test specifically verifies the
        # awaiting-human no-reply path produces zero state churn.
        gh.seed_state(
            2,
            awaiting_human=True,
            last_action_comment_id=900,
            codex_session_id="sess-old",
            user_content_hash=workflow._compute_user_content_hash(
                issue, set()
            ),
        )
        before = gh.write_state_calls

        mocks = self._run(
            lambda: workflow._handle_implementing(gh, _TEST_SPEC, issue),
            run_agent=_agent(),
        )

        mocks["run_agent"].assert_not_called()
        self.assertEqual(gh.write_state_calls, before)
        # Pinned data unchanged.
        self.assertTrue(gh.pinned_data(2).get("awaiting_human"))
        self.assertEqual(gh.pinned_data(2).get("codex_session_id"), "sess-old")

    def test_new_comments_resume_with_session_and_clear_awaiting(self) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(2, label="implementing")
        issue.comments.append(
            FakeComment(id=1100, body="please use sqlite", user=FakeUser("alice"))
        )
        gh.add_issue(issue)
        gh.seed_state(
            2,
            awaiting_human=True,
            last_action_comment_id=900,
            codex_session_id="sess-old",
            branch="orchestrator/geserdugarov__agent-orchestrator/issue-2",
        )

        mocks = self._run(
            lambda: workflow._handle_implementing(gh, _TEST_SPEC, issue),
            run_agent=_agent(session_id="sess-old", last_message="ok"),
            # awaiting_human path skips the recovered-worktree probe; only
            # the post-codex commit check runs.
            has_new_commits=[True],
            dirty_files=(),
            push_branch=True,
        )

        mocks["run_agent"].assert_called_once()
        call = mocks["run_agent"].call_args
        # Legacy `codex_session_id` locks the resume to the codex backend
        # regardless of the current DEV_AGENT default.
        self.assertEqual(call.args[0], "codex")
        self.assertEqual(call.kwargs.get("resume_session_id"), "sess-old")
        followup_arg = call.args[1]
        self.assertIn("please use sqlite", followup_arg)
        # The bare human-reply followup must still carry the
        # foreground-only execution-model note -- a resumed dev that
        # backgrounds a slow test run and ends its turn "to check later"
        # strands the issue (the job dies with the session).
        self.assertIn("NEVER start a background job", followup_arg)
        # Ran through to PR open.
        self.assertEqual(len(gh.opened_prs), 1)
        self.assertFalse(gh.pinned_data(2).get("awaiting_human"))


class HandleImplementingInterruptedTest(unittest.TestCase, _PatchedWorkflowMixin):
    """A dev run the shutdown sweep killed mid-flight (`AgentResult.interrupted`)
    must be ignored: the handler returns quietly WITHOUT writing pinned state,
    so durable GitHub state stays retryable by the next process. It must not
    park, post a HITL question, consume `awaiting_human`, advance the
    action watermark, or open a PR off a partial result."""

    def test_awaiting_human_resume_interrupted_leaves_state_untouched(
        self,
    ) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(70, label="implementing")
        issue.comments.append(
            FakeComment(id=1100, body="please use sqlite", user=FakeUser("alice"))
        )
        gh.add_issue(issue)
        gh.seed_state(
            70,
            awaiting_human=True,
            last_action_comment_id=900,
            codex_session_id="sess-old",
            branch="orchestrator/geserdugarov__agent-orchestrator/issue-70",
            user_content_hash=workflow._compute_user_content_hash(issue, set()),
        )
        before_writes = gh.write_state_calls

        mocks = self._run(
            lambda: workflow._handle_implementing(gh, _TEST_SPEC, issue),
            run_agent=_agent(session_id="sess-old", interrupted=True),
        )

        # The resume DID spawn -- the interruption is observed only after
        # the agent returns.
        mocks["run_agent"].assert_called_once()
        # No durable state churn: the in-memory `awaiting_human=False` /
        # watermark-bump / session writes are all discarded.
        self.assertEqual(gh.write_state_calls, before_writes)
        data = gh.pinned_data(70)
        self.assertTrue(data.get("awaiting_human"))
        self.assertEqual(data.get("last_action_comment_id"), 900)
        # No PR, no label flip, no HITL question / timeout park comment.
        self.assertEqual(gh.opened_prs, [])
        self.assertEqual(gh.label_history, [])
        self.assertFalse(any(
            "agent needs your input" in body or "timed out" in body
            for _, body in gh.posted_comments
        ))

    def test_fresh_spawn_interrupted_does_not_persist_session_or_pr(
        self,
    ) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(71, label="implementing")
        gh.add_issue(issue)
        # Seed the content hash so the first-encounter drift baseline write
        # doesn't fire -- this test asserts ZERO state writes.
        gh.seed_state(
            71, user_content_hash=workflow._compute_user_content_hash(issue, set()),
        )
        before_writes = gh.write_state_calls

        mocks = self._run(
            lambda: workflow._handle_implementing(gh, _TEST_SPEC, issue),
            run_agent=_agent(session_id="sess-new", interrupted=True),
            # First probe: not a recovered worktree -> the dev runs and is
            # then seen to be interrupted; the post-agent commit check must
            # never be reached.
            has_new_commits=[False],
        )

        mocks["run_agent"].assert_called_once()
        self.assertEqual(gh.write_state_calls, before_writes)
        self.assertEqual(gh.opened_prs, [])
        self.assertEqual(gh.label_history, [])
        data = gh.pinned_data(71)
        # The interrupted spawn's session id is NOT persisted -- the next
        # process re-spawns fresh rather than resuming a half-built session.
        self.assertNotIn("dev_session_id", data)


class HandleImplementingRecoveredWorktreeTest(unittest.TestCase, _PatchedWorkflowMixin):
    def test_recovered_worktree_skips_codex_and_pushes(self) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(3, label="implementing")
        gh.add_issue(issue)
        gh.seed_state(3, codex_session_id="sess-prev")

        mocks = self._run(
            lambda: workflow._handle_implementing(gh, _TEST_SPEC, issue),
            run_agent=_agent(),
            has_new_commits=True,
            dirty_files=(),
            push_branch=True,
        )

        mocks["run_agent"].assert_not_called()
        mocks["_push_branch"].assert_called_once()
        self.assertEqual(len(gh.opened_prs), 1)
        # Prior session id retained.
        self.assertEqual(gh.pinned_data(3).get("codex_session_id"), "sess-prev")
