# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("ORCHESTRATOR_SKIP_DOTENV", "1")

from orchestrator import config, workflow, worktrees

from tests.fakes import FakeComment, FakeGitHubClient, make_issue
from tests.workflow_helpers import (
    _PatchedWorkflowMixin,
    _TEST_SPEC,
    _agent,
)


class HandleQuestionFreshRunTest(unittest.TestCase, _PatchedWorkflowMixin):
    """First-tick spawn paths: the question handler runs the configured
    `DECOMPOSE_AGENT` in the per-issue worktree (`issue-N`), posts the
    answer back to the issue thread, persists the agent / session, and
    parks awaiting human. The agent must never push, open a PR, or
    relabel the issue.
    """

    def _seeded(self) -> tuple[FakeGitHubClient, object]:
        gh = FakeGitHubClient()
        issue = make_issue(1, label="question", body="Where does X live?")
        gh.add_issue(issue)
        return gh, issue

    def test_answer_posts_comment_and_parks_awaiting_human(self) -> None:
        gh, issue = self._seeded()
        mocks = self._run(
            lambda: workflow._handle_question(gh, _TEST_SPEC, issue),
            run_agent=_agent(
                session_id="q-sess-1",
                last_message="X lives in src/x.py:42.",
            ),
            has_new_commits=False,
        )

        # Read-only stage: no push, no PR, no relabel.
        mocks["_push_branch"].assert_not_called()
        self.assertEqual(gh.opened_prs, [])
        self.assertEqual(gh.label_history, [])

        # The answer was posted to the issue thread pinging HITL_MENTIONS.
        self.assertEqual(len(gh.posted_comments), 1)
        _, body = gh.posted_comments[0]
        self.assertIn(config.HITL_MENTIONS, body)
        self.assertIn("> X lives in src/x.py:42.", body)

        # Pinned state records the agent spec, session id, and park reason.
        data = gh.pinned_data(1)
        self.assertEqual(data["question_agent"], config.DECOMPOSE_AGENT_SPEC)
        self.assertEqual(data["question_session_id"], "q-sess-1")
        self.assertTrue(data["awaiting_human"])
        self.assertEqual(data["park_reason"], "question_answer")
        self.assertIn("last_question_at", data)

        # The agent ran in the per-issue worktree, not the decomposer one.
        mocks["_ensure_worktree"].assert_called_once_with(
            _TEST_SPEC, 1,
            branch="orchestrator/geserdugarov__agent-orchestrator/issue-1",
        )
        mocks["_ensure_decompose_worktree"].assert_not_called()

    def test_uses_decompose_agent_backend(self) -> None:
        # Locked-backend pattern: the persisted spec is the configured
        # DECOMPOSE_AGENT spec. The orchestrator does not flip to a
        # different backend mid-conversation, and a later env flip cannot
        # retarget the resume at the wrong CLI.
        gh, issue = self._seeded()
        mocks = self._run(
            lambda: workflow._handle_question(gh, _TEST_SPEC, issue),
            run_agent=_agent(last_message="answer text"),
        )
        call_kwargs = mocks["run_agent"].call_args.kwargs
        self.assertEqual(
            mocks["run_agent"].call_args.args[0], config.DECOMPOSE_AGENT,
        )
        self.assertEqual(
            call_kwargs.get("extra_args"), config.DECOMPOSE_AGENT_ARGS,
        )

    def test_question_stage_does_not_count_against_retry_budget(self) -> None:
        # Mirrors the implementing/decomposing retry-budget contract --
        # but the question stage explicitly does NOT consume that budget,
        # since the agent does no codegen and a wedged conversation does
        # not threaten an issue's daily spawn allowance.
        gh, issue = self._seeded()
        with patch.object(workflow, "_check_and_increment_retry_budget") as cb:
            self._run(
                lambda: workflow._handle_question(gh, _TEST_SPEC, issue),
                run_agent=_agent(last_message="answer"),
            )
        cb.assert_not_called()


class HandleQuestionParkPathsTest(unittest.TestCase, _PatchedWorkflowMixin):
    """The handler distinguishes four park reasons -- timeout, silent
    crash, dirty worktree, and commit -- so an operator can tell why
    the conversation stalled. All four leave `awaiting_human=True`
    and no PR / no push / no relabel.
    """

    def _seeded(self) -> tuple[FakeGitHubClient, object]:
        gh = FakeGitHubClient()
        issue = make_issue(2, label="question")
        gh.add_issue(issue)
        return gh, issue

    def _assert_no_pr_no_push_no_relabel(self, gh, mocks) -> None:
        mocks["_push_branch"].assert_not_called()
        self.assertEqual(gh.opened_prs, [])
        self.assertEqual(gh.label_history, [])

    def test_timeout_parks_with_question_timeout_reason(self) -> None:
        gh, issue = self._seeded()
        mocks = self._run(
            lambda: workflow._handle_question(gh, _TEST_SPEC, issue),
            run_agent=_agent(timed_out=True, last_message=""),
        )
        self._assert_no_pr_no_push_no_relabel(gh, mocks)
        data = gh.pinned_data(2)
        self.assertTrue(data["awaiting_human"])
        self.assertEqual(data["park_reason"], "question_timeout")
        self.assertIn(config.HITL_MENTIONS, gh.posted_comments[-1][1])
        self.assertIn("timed out", gh.posted_comments[-1][1])

    def test_silent_run_parks_with_question_silent_reason(self) -> None:
        # No commit AND no final message -- distinct from a real
        # clarifying question; see the implementer's `_on_question`
        # silent branch for the parallel.
        gh, issue = self._seeded()
        mocks = self._run(
            lambda: workflow._handle_question(gh, _TEST_SPEC, issue),
            run_agent=_agent(
                last_message="", exit_code=1, stderr="something broke",
            ),
        )
        self._assert_no_pr_no_push_no_relabel(gh, mocks)
        data = gh.pinned_data(2)
        self.assertEqual(data["park_reason"], "question_silent")
        # Silent-path park surfaces stderr diagnostics for the operator.
        self.assertIn("something broke", gh.posted_comments[-1][1])

    def test_commit_output_parks_without_pushing(self) -> None:
        # The question stage is read-only. A commit is misbehavior --
        # park with question_commits, keep the issue on label `question`,
        # and refuse to push.
        gh, issue = self._seeded()
        mocks = self._run(
            lambda: workflow._handle_question(gh, _TEST_SPEC, issue),
            run_agent=_agent(last_message="here is a code change"),
            has_new_commits=True,
        )
        self._assert_no_pr_no_push_no_relabel(gh, mocks)
        data = gh.pinned_data(2)
        self.assertEqual(data["park_reason"], "question_commits")
        self.assertIn("read-only", gh.posted_comments[-1][1])

    def test_dirty_worktree_parks_without_pushing(self) -> None:
        gh, issue = self._seeded()
        dirty = [f"file_{i}.py" for i in range(15)]
        mocks = self._run(
            lambda: workflow._handle_question(gh, _TEST_SPEC, issue),
            run_agent=_agent(last_message="changes left in tree"),
            has_new_commits=False,
            dirty_files=dirty,
        )
        self._assert_no_pr_no_push_no_relabel(gh, mocks)
        data = gh.pinned_data(2)
        self.assertEqual(data["park_reason"], "question_dirty")
        comment = gh.posted_comments[-1][1]
        self.assertIn("file_0.py", comment)
        self.assertIn("file_9.py", comment)
        self.assertNotIn("file_10.py", comment)
        self.assertIn("(5 more)", comment)


class HandleQuestionAwaitingHumanResumeTest(
    unittest.TestCase, _PatchedWorkflowMixin,
):
    """Once the agent has parked awaiting human, a new comment on the
    issue resumes the locked-backend session with the human's reply
    and re-posts the next answer. No reply means the handler returns
    without spawning the agent.
    """

    def test_no_new_comments_returns_without_spawning(self) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(3, label="question")
        gh.add_issue(issue)
        gh.seed_state(
            3,
            awaiting_human=True,
            last_action_comment_id=9999,
            question_agent="claude",
            question_session_id="q-sess-prior",
            park_reason="question_answer",
        )
        mocks = self._run(
            lambda: workflow._handle_question(gh, _TEST_SPEC, issue),
            run_agent=_agent(last_message="should not run"),
        )
        mocks["run_agent"].assert_not_called()
        # No fresh comment, no relabel, no PR.
        self.assertEqual(gh.posted_comments, [])
        self.assertEqual(gh.label_history, [])
        self.assertEqual(gh.opened_prs, [])

    def test_new_comment_resumes_locked_session_and_advances_watermark(
        self,
    ) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(4, label="question")
        # Human reply with id strictly greater than the prior watermark.
        issue.comments.append(FakeComment(id=12000, body="please clarify Y"))
        gh.add_issue(issue)
        gh.seed_state(
            4,
            awaiting_human=True,
            last_action_comment_id=11000,
            question_agent="claude",
            question_session_id="q-sess-prior",
            park_reason="question_answer",
        )
        mocks = self._run(
            lambda: workflow._handle_question(gh, _TEST_SPEC, issue),
            run_agent=_agent(
                session_id="q-sess-prior",
                last_message="Y is defined in y.py.",
            ),
        )
        # Resume hit the locked session id of the prior tick.
        spawn_args = mocks["run_agent"].call_args.args
        spawn_kwargs = mocks["run_agent"].call_args.kwargs
        self.assertEqual(spawn_kwargs.get("resume_session_id"), "q-sess-prior")
        # The resume prompt (positional arg 1) quotes the human's reply
        # so the agent has the new context inline.
        self.assertIn("please clarify Y", spawn_args[1])
        # Watermark advanced past the consumed comment so the next tick
        # without a new reply is a no-op.
        data = gh.pinned_data(4)
        self.assertGreaterEqual(data["last_action_comment_id"], 12000)
        # The follow-up answer was posted and the issue re-parks awaiting
        # human (so the human can either answer again or close / relabel).
        self.assertTrue(data["awaiting_human"])
        self.assertEqual(data["park_reason"], "question_answer")
        self.assertIn("Y is defined in y.py.", gh.posted_comments[-1][1])

    def test_multi_round_qa_advances_watermark_each_tick(self) -> None:
        # Three-round conversation: fresh spawn answers Q1, human asks
        # Q2, agent answers Q2, human asks Q3, agent answers Q3.
        # Each round the watermark must advance past the orchestrator's
        # OWN answer comment so the next no-reply tick is a no-op (i.e.
        # bot comments do not feed back into the resume loop) AND past
        # the consumed human comment so the same reply is not replayed.
        gh = FakeGitHubClient()
        issue = make_issue(40, label="question", body="open question?")
        gh.add_issue(issue)

        # Round 1: fresh spawn.
        self._run(
            lambda: workflow._handle_question(gh, _TEST_SPEC, issue),
            run_agent=_agent(
                session_id="q-sess-rolling",
                last_message="round-1 answer",
            ),
            has_new_commits=False,
        )
        data = gh.pinned_data(40)
        self.assertTrue(data["awaiting_human"])
        self.assertEqual(data["park_reason"], "question_answer")
        wm_after_r1 = data["last_action_comment_id"]
        # Watermark is at or past the orchestrator's just-posted answer
        # comment (the one carrying the answer body). The subsequent
        # pinned-state comment also lives on the issue but is filtered
        # out of `comments_after` by its marker, so the relevant id to
        # compare against is the answer comment, not the latest overall.
        answer_comments = [
            c for c in issue.comments if "round-1 answer" in (c.body or "")
        ]
        self.assertEqual(len(answer_comments), 1)
        self.assertGreaterEqual(wm_after_r1, answer_comments[0].id)
        self.assertEqual(data["question_session_id"], "q-sess-rolling")

        # A no-reply tick between rounds must be a no-op: the
        # orchestrator's own comment is below the watermark and
        # `comments_after` returns nothing.
        mocks_noop = self._run(
            lambda: workflow._handle_question(gh, _TEST_SPEC, issue),
            run_agent=_agent(last_message="should not run"),
        )
        mocks_noop["run_agent"].assert_not_called()

        # Round 2: human replies.
        issue.comments.append(
            FakeComment(id=wm_after_r1 + 100, body="follow-up Q2"),
        )
        mocks_r2 = self._run(
            lambda: workflow._handle_question(gh, _TEST_SPEC, issue),
            run_agent=_agent(
                session_id="q-sess-rolling",
                last_message="round-2 answer",
            ),
            has_new_commits=False,
        )
        # The resume hit the locked session, NOT a fresh spawn.
        self.assertEqual(
            mocks_r2["run_agent"].call_args.kwargs.get("resume_session_id"),
            "q-sess-rolling",
        )
        # The prompt quoted the new human reply, not the prior bot answer.
        prompt_r2 = mocks_r2["run_agent"].call_args.args[1]
        self.assertIn("follow-up Q2", prompt_r2)
        self.assertNotIn("round-1 answer", prompt_r2)
        data = gh.pinned_data(40)
        self.assertTrue(data["awaiting_human"])
        self.assertEqual(data["park_reason"], "question_answer")
        wm_after_r2 = data["last_action_comment_id"]
        self.assertGreater(wm_after_r2, wm_after_r1)

        # Round 3: another human reply.
        issue.comments.append(
            FakeComment(id=wm_after_r2 + 100, body="follow-up Q3"),
        )
        mocks_r3 = self._run(
            lambda: workflow._handle_question(gh, _TEST_SPEC, issue),
            run_agent=_agent(
                session_id="q-sess-rolling",
                last_message="round-3 answer",
            ),
            has_new_commits=False,
        )
        self.assertEqual(
            mocks_r3["run_agent"].call_args.kwargs.get("resume_session_id"),
            "q-sess-rolling",
        )
        prompt_r3 = mocks_r3["run_agent"].call_args.args[1]
        self.assertIn("follow-up Q3", prompt_r3)
        # The prior bot answers did not leak into the resume prompt.
        self.assertNotIn("round-1 answer", prompt_r3)
        self.assertNotIn("round-2 answer", prompt_r3)
        data = gh.pinned_data(40)
        wm_after_r3 = data["last_action_comment_id"]
        self.assertGreater(wm_after_r3, wm_after_r2)

        # All three orchestrator answer comments were posted to the
        # issue thread (and the issue carries them plus the two human
        # replies). The agent only ran three times across the three
        # rounds; the no-reply tick in between did not spawn it.
        answer_bodies = [body for _, body in gh.posted_comments]
        self.assertEqual(
            sum(1 for b in answer_bodies if "round-1 answer" in b), 1,
        )
        self.assertEqual(
            sum(1 for b in answer_bodies if "round-2 answer" in b), 1,
        )
        self.assertEqual(
            sum(1 for b in answer_bodies if "round-3 answer" in b), 1,
        )


class HandleQuestionClosedIssueTerminalTest(
    unittest.TestCase, _PatchedWorkflowMixin,
):
    """A human closing a `question`-labeled issue is the terminal
    signal: `_handle_question` must NOT spawn the agent, must stamp
    terminal state, flip the workflow label to `done`, and clean up
    the per-issue worktree + local branch via
    `_cleanup_question_worktree`.

    The closed-issue sweep in `list_pollable_issues` is what surfaces
    the closed `question` issue here; once we flip the label to `done`
    the sweep no longer yields it and the cost stays bounded in
    steady state.
    """

    def test_closed_issue_skips_agent_and_finalizes_to_done(self) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(50, label="question")
        issue.closed = True
        gh.add_issue(issue)
        # Mid-conversation state from a prior tick; the close is the
        # terminal signal regardless of where the conversation was.
        gh.seed_state(
            50,
            awaiting_human=True,
            last_action_comment_id=70000,
            question_agent=config.DECOMPOSE_AGENT_SPEC,
            question_session_id="q-sess-prior",
            park_reason="question_answer",
        )
        mocks = self._run(
            lambda: workflow._handle_question(gh, _TEST_SPEC, issue),
            run_agent=_agent(last_message="should not run"),
        )
        mocks["run_agent"].assert_not_called()
        # No new comment posted, no PR, no resume.
        self.assertEqual(gh.posted_comments, [])
        self.assertEqual(gh.opened_prs, [])
        # Workflow label flipped to `done`.
        self.assertEqual(gh.label_history, [(50, "done")])
        # Terminal stamp in pinned state.
        data = gh.pinned_data(50)
        self.assertIn("question_closed_at", data)
        # Cleanup ran.
        mocks["_cleanup_question_worktree"].assert_called_once_with(
            _TEST_SPEC, 50,
            branch="orchestrator/geserdugarov__agent-orchestrator/issue-50",
        )

    def test_closed_issue_with_unsafe_park_still_cleans_up(self) -> None:
        # When the operator closes an issue parked with an unsafe
        # park reason (commits / dirty / timeout left the worktree
        # intact for inspection), closing IS the operator's "I'm
        # done with this" signal -- the inspection window ends and
        # cleanup runs unconditionally.
        gh = FakeGitHubClient()
        issue = make_issue(51, label="question")
        issue.closed = True
        gh.add_issue(issue)
        gh.seed_state(
            51,
            awaiting_human=True,
            park_reason="question_commits",
            question_agent=config.DECOMPOSE_AGENT_SPEC,
            question_session_id="q-sess-prior",
            last_action_comment_id=71000,
        )
        mocks = self._run(
            lambda: workflow._handle_question(gh, _TEST_SPEC, issue),
            run_agent=_agent(last_message="should not run"),
        )
        mocks["run_agent"].assert_not_called()
        self.assertEqual(gh.label_history, [(51, "done")])
        mocks["_cleanup_question_worktree"].assert_called_once_with(
            _TEST_SPEC, 51,
            branch="orchestrator/geserdugarov__agent-orchestrator/issue-51",
        )

    def test_closed_issue_without_prior_state_finalizes_cleanly(self) -> None:
        # No pinned state at all -- e.g. the issue was labeled
        # `question` and immediately closed before the orchestrator
        # spawned anything. The terminal handler still finalizes
        # cleanly: no agent spawn, label flips to `done`, cleanup
        # runs (idempotent best-effort if nothing exists on disk).
        gh = FakeGitHubClient()
        issue = make_issue(52, label="question")
        issue.closed = True
        gh.add_issue(issue)
        mocks = self._run(
            lambda: workflow._handle_question(gh, _TEST_SPEC, issue),
            run_agent=_agent(last_message="should not run"),
        )
        mocks["run_agent"].assert_not_called()
        self.assertEqual(gh.label_history, [(52, "done")])
        data = gh.pinned_data(52)
        self.assertIn("question_closed_at", data)
        mocks["_cleanup_question_worktree"].assert_called_once_with(
            _TEST_SPEC, 52,
            branch="orchestrator/geserdugarov__agent-orchestrator/issue-52",
        )


class HandleQuestionWorktreeCleanupTest(
    unittest.TestCase, _PatchedWorkflowMixin,
):
    """The read-only question stage must not leave a per-issue
    worktree on disk between ticks: `_refresh_base_and_worktrees`
    would otherwise merge `origin/<base>` into the pre-PR worktree,
    accreting commits on a branch the question agent is forbidden
    from touching, and a later relabel to `implementing` would then
    either trip the `question_unsafe_relabel` guard or fall through
    to the recovered-worktree push path. Every safe-exit of
    `_handle_question` therefore tears the worktree down via
    `_cleanup_question_worktree`. The unsafe parks
    (`question_commits`, `question_dirty`, `question_timeout`) keep
    the worktree so the operator can inspect.
    """

    def _seeded(self, number: int = 100) -> tuple[FakeGitHubClient, object]:
        gh = FakeGitHubClient()
        issue = make_issue(number, label="question")
        gh.add_issue(issue)
        return gh, issue

    def test_answer_path_cleans_up_worktree(self) -> None:
        gh, issue = self._seeded(100)
        mocks = self._run(
            lambda: workflow._handle_question(gh, _TEST_SPEC, issue),
            run_agent=_agent(last_message="here is the answer"),
            has_new_commits=False,
        )
        mocks["_cleanup_question_worktree"].assert_called_once_with(
            _TEST_SPEC, 100,
            branch="orchestrator/geserdugarov__agent-orchestrator/issue-100",
        )

    def test_silent_path_cleans_up_worktree(self) -> None:
        gh, issue = self._seeded(101)
        mocks = self._run(
            lambda: workflow._handle_question(gh, _TEST_SPEC, issue),
            run_agent=_agent(last_message="", exit_code=1),
            has_new_commits=False,
        )
        mocks["_cleanup_question_worktree"].assert_called_once_with(
            _TEST_SPEC, 101,
            branch="orchestrator/geserdugarov__agent-orchestrator/issue-101",
        )

    def test_resume_no_new_comments_still_cleans_stale_worktree(
        self,
    ) -> None:
        # A no-reply tick must still tear down any worktree left by
        # a prior tick. Without this, an answered question that the
        # operator left alone for a few ticks would accumulate base
        # merges in the worktree even though `_handle_question`
        # itself did nothing.
        gh = FakeGitHubClient()
        issue = make_issue(102, label="question")
        gh.add_issue(issue)
        gh.seed_state(
            102,
            awaiting_human=True,
            last_action_comment_id=99999,
            question_agent="claude",
            question_session_id="q-sess-stale",
            park_reason="question_answer",
        )
        mocks = self._run(
            lambda: workflow._handle_question(gh, _TEST_SPEC, issue),
            run_agent=_agent(last_message="should not run"),
        )
        mocks["run_agent"].assert_not_called()
        mocks["_cleanup_question_worktree"].assert_called_once_with(
            _TEST_SPEC, 102,
            branch="orchestrator/geserdugarov__agent-orchestrator/issue-102",
        )

    def test_timeout_park_keeps_worktree_for_inspection(self) -> None:
        gh, issue = self._seeded(103)
        mocks = self._run(
            lambda: workflow._handle_question(gh, _TEST_SPEC, issue),
            run_agent=_agent(timed_out=True, last_message=""),
        )
        mocks["_cleanup_question_worktree"].assert_not_called()
        data = gh.pinned_data(103)
        self.assertEqual(data["park_reason"], "question_timeout")
        self.assertIn("worktree is left intact", gh.posted_comments[-1][1])

    def test_commits_park_keeps_worktree_for_inspection(self) -> None:
        gh, issue = self._seeded(104)
        mocks = self._run(
            lambda: workflow._handle_question(gh, _TEST_SPEC, issue),
            run_agent=_agent(last_message="here is a code change"),
            has_new_commits=True,
        )
        mocks["_cleanup_question_worktree"].assert_not_called()
        data = gh.pinned_data(104)
        self.assertEqual(data["park_reason"], "question_commits")

    def test_dirty_park_keeps_worktree_for_inspection(self) -> None:
        gh, issue = self._seeded(105)
        mocks = self._run(
            lambda: workflow._handle_question(gh, _TEST_SPEC, issue),
            run_agent=_agent(last_message="dropped changes"),
            has_new_commits=False,
            dirty_files=["src/x.py"],
        )
        mocks["_cleanup_question_worktree"].assert_not_called()
        data = gh.pinned_data(105)
        self.assertEqual(data["park_reason"], "question_dirty")


class HandleQuestionUnsafeParkStabilityTest(
    unittest.TestCase, _PatchedWorkflowMixin,
):
    """An unsafe question-stage park (`question_commits`,
    `question_dirty`, `question_timeout`) explicitly LEAVES the
    per-issue worktree on disk so the operator can inspect what the
    misbehaving agent did. A no-reply tick on that parked state
    must NOT silently tear down the inspection target: the
    awaiting-human branch returns early without producing a new
    park decision, and the `finally` block has to carry over the
    prior tick's preservation rather than reset to clean.
    """

    def _seeded_unsafe(
        self, number: int, park_reason: str,
    ) -> tuple[FakeGitHubClient, object]:
        gh = FakeGitHubClient()
        issue = make_issue(number, label="question")
        gh.add_issue(issue)
        gh.seed_state(
            number,
            awaiting_human=True,
            park_reason=park_reason,
            question_agent=config.DECOMPOSE_AGENT_SPEC,
            question_session_id="q-sess-prior",
            last_action_comment_id=88888,
        )
        return gh, issue

    def test_no_reply_after_question_commits_preserves_worktree(
        self,
    ) -> None:
        gh, issue = self._seeded_unsafe(300, "question_commits")
        mocks = self._run(
            lambda: workflow._handle_question(gh, _TEST_SPEC, issue),
            run_agent=_agent(last_message="should not run"),
        )
        mocks["run_agent"].assert_not_called()
        mocks["_cleanup_question_worktree"].assert_not_called()

    def test_no_reply_after_question_dirty_preserves_worktree(self) -> None:
        gh, issue = self._seeded_unsafe(301, "question_dirty")
        mocks = self._run(
            lambda: workflow._handle_question(gh, _TEST_SPEC, issue),
            run_agent=_agent(last_message="should not run"),
        )
        mocks["run_agent"].assert_not_called()
        mocks["_cleanup_question_worktree"].assert_not_called()

    def test_no_reply_after_question_timeout_preserves_worktree(
        self,
    ) -> None:
        gh, issue = self._seeded_unsafe(302, "question_timeout")
        mocks = self._run(
            lambda: workflow._handle_question(gh, _TEST_SPEC, issue),
            run_agent=_agent(last_message="should not run"),
        )
        mocks["run_agent"].assert_not_called()
        mocks["_cleanup_question_worktree"].assert_not_called()

    def test_no_reply_after_safe_park_still_cleans_stale_worktree(
        self,
    ) -> None:
        # Counter-test: the preservation must only apply to UNSAFE
        # parks. A no-reply tick on a `question_answer` park still
        # cleans up a stale worktree from a previous tick (this is
        # what `test_resume_no_new_comments_still_cleans_stale_worktree`
        # in the cleanup-test class covers; restating it here keeps
        # the read of the stability class self-contained).
        gh, issue = self._seeded_unsafe(303, "question_answer")
        mocks = self._run(
            lambda: workflow._handle_question(gh, _TEST_SPEC, issue),
            run_agent=_agent(last_message="should not run"),
        )
        mocks["run_agent"].assert_not_called()
        mocks["_cleanup_question_worktree"].assert_called_once_with(
            _TEST_SPEC, 303,
            branch="orchestrator/geserdugarov__agent-orchestrator/issue-303",
        )

    def test_resume_after_unsafe_park_with_clean_answer_cleans_up(
        self,
    ) -> None:
        # When the operator resets the worktree and replies, the
        # resumed agent's clean answer (no new commits / dirty)
        # ENDS the inspection window: the worktree is provably
        # safe to reap. Without the explicit `keep_worktree =
        # False` reset on the answer branch, the prior unsafe
        # park would keep preserving forever.
        gh = FakeGitHubClient()
        issue = make_issue(304, label="question")
        issue.comments.append(
            FakeComment(id=99000, body="i reset the worktree, retry"),
        )
        gh.add_issue(issue)
        gh.seed_state(
            304,
            awaiting_human=True,
            park_reason="question_commits",
            question_agent=config.DECOMPOSE_AGENT_SPEC,
            question_session_id="q-sess-prior",
            last_action_comment_id=88888,
        )
        mocks = self._run(
            lambda: workflow._handle_question(gh, _TEST_SPEC, issue),
            run_agent=_agent(
                session_id="q-sess-prior",
                last_message="ok, here is the actual answer",
            ),
            has_new_commits=False,
            dirty_files=(),
        )
        # Agent ran (human replied) and produced a clean answer.
        mocks["run_agent"].assert_called_once()
        data = gh.pinned_data(304)
        self.assertEqual(data["park_reason"], "question_answer")
        # Worktree is now safe to reap.
        mocks["_cleanup_question_worktree"].assert_called_once_with(
            _TEST_SPEC, 304,
            branch="orchestrator/geserdugarov__agent-orchestrator/issue-304",
        )

    def test_resume_after_unsafe_park_with_re_park_preserves_worktree(
        self,
    ) -> None:
        # When the operator replies without resetting (and the
        # leftover commits are still in the worktree), the resumed
        # agent's run lands on _has_new_commits=True and re-parks
        # as `question_commits` -- preservation continues.
        gh = FakeGitHubClient()
        issue = make_issue(305, label="question")
        issue.comments.append(
            FakeComment(id=99500, body="why did you commit?"),
        )
        gh.add_issue(issue)
        gh.seed_state(
            305,
            awaiting_human=True,
            park_reason="question_commits",
            question_agent=config.DECOMPOSE_AGENT_SPEC,
            question_session_id="q-sess-prior",
            last_action_comment_id=88888,
        )
        mocks = self._run(
            lambda: workflow._handle_question(gh, _TEST_SPEC, issue),
            run_agent=_agent(
                session_id="q-sess-prior",
                last_message="i had to commit",
            ),
            has_new_commits=True,
        )
        mocks["run_agent"].assert_called_once()
        data = gh.pinned_data(305)
        self.assertEqual(data["park_reason"], "question_commits")
        mocks["_cleanup_question_worktree"].assert_not_called()


class QuestionLabelBaseRefreshSkipTest(unittest.TestCase):
    """Defense in depth: even when `_handle_question` keeps a
    worktree on disk for one of the unsafe parks (`question_*`
    where the operator must inspect before resetting), the per-tick
    `_refresh_base_and_worktrees` must NOT merge `origin/<base>`
    over that inspection state. The base-sync helper short-circuits
    on the `question` workflow label.
    """

    def test_question_labeled_issue_skips_base_sync(self) -> None:
        from orchestrator import base_sync

        gh = FakeGitHubClient()
        issue = make_issue(200, label="question")
        gh.add_issue(issue)

        # The merge / rev-list helpers would shell out if reached;
        # patch them so a regression that lets the sync proceed
        # surfaces as a call on these mocks.
        with patch.object(base_sync, "_git") as git_mock, \
             patch.object(
                 base_sync, "_worktree_dirty_files",
                 return_value=[],
             ), \
             patch.object(
                 base_sync, "_merge_base_into_worktree",
                 return_value=(True, []),
             ) as merge_mock:
            base_sync._sync_worktree_with_base(
                gh, _TEST_SPEC, Path("/tmp/q-issue-200"), 200,
            )

        # Neither the rev-list (used to decide whether to merge) nor
        # the merge helper itself runs for a question-labeled issue.
        git_mock.assert_not_called()
        merge_mock.assert_not_called()


class QuestionRelabelToImplementingTest(
    unittest.TestCase, _PatchedWorkflowMixin,
):
    """Operator relabels a parked `question` issue to `implementing`.

    `_handle_question` parks with `awaiting_human=True` and
    `park_reason="question_*"` so its own next tick can resume the
    locked question-agent session. Those flags are opaque to
    `_handle_implementing`'s resume path; without the
    question-stage-park clear at the top of that handler, the
    awaiting_human branch either no-ops (no new comments since the
    question agent's answer) or fresh-spawns the dev with only the
    human's reply as the prompt rather than a real implement prompt.
    """

    def test_relabel_clears_question_park_and_runs_fresh_implement(
        self,
    ) -> None:
        gh = FakeGitHubClient()
        # Issue is now labeled `implementing` (the operator relabeled)
        # but the pinned state still carries the question stage's
        # awaiting_human / park_reason from the prior tick.
        issue = make_issue(80, label="implementing")
        gh.add_issue(issue)
        gh.seed_state(
            80,
            awaiting_human=True,
            park_reason="question_answer",
            question_agent=config.DECOMPOSE_AGENT_SPEC,
            question_session_id="q-sess-prior",
            last_action_comment_id=40000,
        )

        mocks = self._run(
            lambda: workflow._handle_implementing(gh, _TEST_SPEC, issue),
            run_agent=_agent(
                session_id="dev-sess-1", last_message="implemented",
            ),
            has_new_commits=[False, True],
            push_branch=True,
        )

        # The dev agent ran fresh with the implement prompt (not the
        # question-stage followup), opened a PR, and flipped to
        # validating -- the relabel was honored as an unblock signal.
        mocks["run_agent"].assert_called_once()
        spawn_kwargs = mocks["run_agent"].call_args.kwargs
        # Fresh spawn -- no resume_session_id forwarded.
        self.assertNotIn("resume_session_id", spawn_kwargs)
        prompt = mocks["run_agent"].call_args.args[1]
        self.assertIn("You are the implementer", prompt)

        self.assertEqual(len(gh.opened_prs), 1)
        self.assertIn((80, "validating"), gh.label_history)

        data = gh.pinned_data(80)
        self.assertFalse(data.get("awaiting_human"))
        self.assertIsNone(data.get("park_reason"))

    def test_relabel_with_question_committed_state_refuses_to_push(
        self,
    ) -> None:
        # Regression: the operator relabels from `question` to
        # `implementing` after the question agent's prior tick parked
        # on `question_commits` with unreviewed commits in the
        # worktree. Naively clearing the question-stage park would let
        # the fresh-spawn branch's recovered-worktree shortcut push
        # those commits as if a dev session authored them, violating
        # the read-only contract. The handler must refuse and ask the
        # operator to reset the worktree first.
        with tempfile.TemporaryDirectory(prefix="q-relabel-") as td:
            wt_path = Path(td) / "issue-82"
            wt_path.mkdir()
            gh = FakeGitHubClient()
            issue = make_issue(82, label="implementing")
            gh.add_issue(issue)
            gh.seed_state(
                82,
                awaiting_human=True,
                park_reason="question_commits",
                last_action_comment_id=60000,
            )

            def run() -> None:
                with patch.object(
                    workflow, "_worktree_path", return_value=wt_path,
                ), patch.object(
                    workflow, "_branch_has_unpushed_commits",
                    return_value=(
                        "orchestrator/geserdugarov__agent-orchestrator/issue-82"
                    ),
                ):
                    workflow._handle_implementing(gh, _TEST_SPEC, issue)

            mocks = self._run(
                run,
                run_agent=_agent(last_message="should not run"),
                has_new_commits=True,
            )

        mocks["run_agent"].assert_not_called()
        mocks["_push_branch"].assert_not_called()
        self.assertEqual(gh.opened_prs, [])
        data = gh.pinned_data(82)
        self.assertTrue(data["awaiting_human"])
        self.assertEqual(data["park_reason"], "question_unsafe_relabel")
        last = gh.posted_comments[-1][1]
        self.assertIn("question_commits", last)
        self.assertIn("reset the worktree", last.lower())

    def test_relabel_with_missing_worktree_but_stale_branch_refuses_to_push(
        self,
    ) -> None:
        # Regression: the worktree directory is gone (a prior safe
        # park's `_cleanup_question_worktree` ran, or the operator
        # manually deleted the dir) but the local
        # `orchestrator/issue-N` branch survives with the question
        # agent's commits -- `_cleanup_question_worktree` failed
        # mid-way, or the operator removed only the dir. The
        # worktree-only check would treat the missing path as
        # "clean", let the safe-clear branch fire, and
        # `_ensure_worktree` would restore the branch in a fresh
        # worktree -- the recovered-worktree shortcut would then
        # push the question-agent commits as if a dev session
        # authored them. The branch-level check catches this.
        gh = FakeGitHubClient()
        issue = make_issue(86, label="implementing")
        gh.add_issue(issue)
        gh.seed_state(
            86,
            awaiting_human=True,
            park_reason="question_commits",
            last_action_comment_id=65000,
        )

        def run() -> None:
            # Worktree path that does NOT exist on disk so wt.exists()
            # is False -- the prior worktree-only check would have
            # treated this as safe and cleared.
            missing = Path("/tmp/orchestrator-test-missing-issue-86")
            if missing.exists():
                missing.rmdir()
            with patch.object(
                workflow, "_worktree_path", return_value=missing,
            ), patch.object(
                workflow, "_branch_has_unpushed_commits",
                return_value=(
                    "orchestrator/geserdugarov__agent-orchestrator/issue-86"
                ),
            ):
                workflow._handle_implementing(gh, _TEST_SPEC, issue)

        mocks = self._run(
            run,
            run_agent=_agent(last_message="should not run"),
            has_new_commits=False,
            dirty_files=(),
        )

        # No dev agent ran, no push, no PR -- the branch-level
        # check refused the relabel.
        mocks["run_agent"].assert_not_called()
        mocks["_push_branch"].assert_not_called()
        self.assertEqual(gh.opened_prs, [])
        # State carries the unsafe-relabel park reason.
        data = gh.pinned_data(86)
        self.assertTrue(data["awaiting_human"])
        self.assertEqual(data["park_reason"], "question_unsafe_relabel")
        # Message tells the operator about the branch and how to
        # reset it.
        last = gh.posted_comments[-1][1]
        self.assertIn("question_commits", last)
        self.assertIn("orchestrator/geserdugarov__agent-orchestrator/issue-86", last)
        self.assertIn("git branch -D", last)

    def test_relabel_with_question_dirty_state_refuses_to_push(self) -> None:
        # Same as the commits case, but for `question_dirty`: the
        # question agent left uncommitted edits. Refusal must fire
        # regardless of which read-only-violation path tagged the park.
        with tempfile.TemporaryDirectory(prefix="q-relabel-") as td:
            wt_path = Path(td) / "issue-83"
            wt_path.mkdir()
            gh = FakeGitHubClient()
            issue = make_issue(83, label="implementing")
            gh.add_issue(issue)
            gh.seed_state(
                83,
                awaiting_human=True,
                park_reason="question_dirty",
                last_action_comment_id=70000,
            )

            def run() -> None:
                with patch.object(
                    workflow, "_worktree_path", return_value=wt_path,
                ):
                    workflow._handle_implementing(gh, _TEST_SPEC, issue)

            mocks = self._run(
                run,
                run_agent=_agent(last_message="should not run"),
                has_new_commits=False,
                dirty_files=["src/x.py"],
            )

        mocks["run_agent"].assert_not_called()
        mocks["_push_branch"].assert_not_called()
        data = gh.pinned_data(83)
        self.assertEqual(data["park_reason"], "question_unsafe_relabel")

    def test_unsafe_relabel_tick_is_idempotent_until_worktree_reset(
        self,
    ) -> None:
        # Once the unsafe-relabel re-park has fired, subsequent ticks
        # with the same state must NOT spam a fresh park comment every
        # tick -- the operator has been informed; the only way out is
        # to reset the worktree. The clean-worktree branch fires when
        # the operator actually resets and the handler resumes the
        # normal fresh-spawn flow.
        with tempfile.TemporaryDirectory(prefix="q-relabel-") as td:
            wt_path = Path(td) / "issue-84"
            wt_path.mkdir()
            gh = FakeGitHubClient()
            issue = make_issue(84, label="implementing")
            gh.add_issue(issue)
            gh.seed_state(
                84,
                awaiting_human=True,
                park_reason="question_unsafe_relabel",
                last_action_comment_id=80000,
            )

            def run() -> None:
                with patch.object(
                    workflow, "_worktree_path", return_value=wt_path,
                ), patch.object(
                    workflow, "_branch_has_unpushed_commits",
                    return_value=(
                        "orchestrator/geserdugarov__agent-orchestrator/issue-84"
                    ),
                ):
                    workflow._handle_implementing(gh, _TEST_SPEC, issue)

            mocks = self._run(
                run,
                run_agent=_agent(last_message="should not run"),
                has_new_commits=True,
            )

            self.assertEqual(gh.posted_comments, [])
            mocks["run_agent"].assert_not_called()
            data = gh.pinned_data(84)
            self.assertTrue(data["awaiting_human"])
            self.assertEqual(data["park_reason"], "question_unsafe_relabel")

    def test_unsafe_relabel_recovers_after_worktree_reset(self) -> None:
        # After the operator resets the worktree (no commits, no dirty
        # files), the next tick goes through the safe-clear branch and
        # the dev agent runs fresh -- the unsafe-relabel park is not
        # absorbing the unblock signal.
        with tempfile.TemporaryDirectory(prefix="q-relabel-") as td:
            wt_path = Path(td) / "issue-85"
            wt_path.mkdir()
            gh = FakeGitHubClient()
            issue = make_issue(85, label="implementing")
            gh.add_issue(issue)
            gh.seed_state(
                85,
                awaiting_human=True,
                park_reason="question_unsafe_relabel",
                last_action_comment_id=90000,
            )

            def run() -> None:
                with patch.object(
                    workflow, "_worktree_path", return_value=wt_path,
                ):
                    workflow._handle_implementing(gh, _TEST_SPEC, issue)

            mocks = self._run(
                run,
                run_agent=_agent(
                    session_id="dev-sess-recovered",
                    last_message="implemented",
                ),
                # The unsafe-park branch check uses
                # `_branch_has_unpushed_commits` (default False --
                # the operator reset the local branch too) for the
                # commits half of its safety check, not the
                # worktree's `_has_new_commits`. So only two
                # `_has_new_commits` calls fire: (1) the
                # recovered-worktree check in the fresh-spawn
                # branch sees clean -> agent spawns; (2) the
                # post-agent commit check -> push path.
                has_new_commits=[False, True],
                push_branch=True,
            )

        mocks["run_agent"].assert_called_once()
        spawn_kwargs = mocks["run_agent"].call_args.kwargs
        self.assertNotIn("resume_session_id", spawn_kwargs)
        # The relabel exercises the implementing fresh-spawn path,
        # which now hands off straight to `validating` (no pre-review
        # docs hop).
        self.assertIn((85, "validating"), gh.label_history)
        data = gh.pinned_data(85)
        self.assertFalse(data.get("awaiting_human"))
        self.assertIsNone(data.get("park_reason"))

    def test_relabel_with_no_new_comments_no_longer_no_ops(self) -> None:
        # Regression for the leak: prior to the fix, this scenario
        # would hit implementing's awaiting_human branch,
        # `_resume_developer_on_human_reply` would see no new comments
        # past the question-answer watermark, and the handler would
        # return without spawning anything. The fix clears the stale
        # question-stage park, lets the fresh-spawn branch fire, and
        # the implementation actually starts.
        gh = FakeGitHubClient()
        issue = make_issue(81, label="implementing")
        # No new human comment after the question agent's answer --
        # the operator's only signal was the relabel itself.
        gh.add_issue(issue)
        gh.seed_state(
            81,
            awaiting_human=True,
            park_reason="question_silent",
            last_action_comment_id=50000,
        )
        mocks = self._run(
            lambda: workflow._handle_implementing(gh, _TEST_SPEC, issue),
            run_agent=_agent(last_message="needs clarification"),
            has_new_commits=False,
        )
        # Dev agent ran (the relabel was honored).
        mocks["run_agent"].assert_called_once()


class HandleQuestionSessionPersistenceTest(
    unittest.TestCase, _PatchedWorkflowMixin,
):
    """The agent spec is persisted BEFORE the spawn so a CLI hiccup that
    surfaces no session id cannot orphan the role identity. A later
    DECOMPOSE_AGENT env flip then cannot retarget the resume at the
    wrong backend.
    """

    def test_spec_persisted_even_when_run_returns_no_session_id(self) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(5, label="question")
        gh.add_issue(issue)
        self._run(
            lambda: workflow._handle_question(gh, _TEST_SPEC, issue),
            run_agent=_agent(session_id="", last_message="best-effort answer"),
        )
        data = gh.pinned_data(5)
        self.assertEqual(data["question_agent"], config.DECOMPOSE_AGENT_SPEC)
        # No session id was returned -- the field is absent / falsy, but
        # the role identity is still durable.
        self.assertFalse(data.get("question_session_id"))

    def test_resume_without_session_id_uses_full_question_prompt(
        self,
    ) -> None:
        # Regression: when `question_session_id` is missing (a prior
        # CLI hiccup left no captured id), `_run_agent_tracked`
        # starts a FRESH agent rather than resuming an existing
        # session. The followup-only prompt assumes a live session
        # has the issue body / title / prior conversation cached;
        # passing it to a fresh agent leaves it with nothing to
        # answer against. The handler must spawn with the full
        # question prompt in this branch so the recovery run sees
        # the same context a first-tick run would.
        gh = FakeGitHubClient()
        issue = make_issue(
            55,
            label="question",
            title="Where does X live?",
            body="We need to know where X lives in the codebase.",
        )
        issue.comments.append(
            FakeComment(id=42000, body="any progress on this?"),
        )
        gh.add_issue(issue)
        gh.seed_state(
            55,
            awaiting_human=True,
            last_action_comment_id=41000,
            question_agent=config.DECOMPOSE_AGENT_SPEC,
            # No prior session id -- the prior run hiccupped.
            park_reason="question_answer",
        )
        mocks = self._run(
            lambda: workflow._handle_question(gh, _TEST_SPEC, issue),
            run_agent=_agent(
                session_id="q-sess-fresh",
                last_message="X lives in src/x.py",
            ),
        )
        # The agent ran without a resume_session_id (fresh spawn).
        spawn_args = mocks["run_agent"].call_args.args
        spawn_kwargs = mocks["run_agent"].call_args.kwargs
        self.assertIsNone(spawn_kwargs.get("resume_session_id"))
        # The spawn prompt is the FULL question prompt: issue body,
        # title, and conversation are all present so the fresh
        # agent has the same context a first-tick spawn would. The
        # human's new reply is included via the conversation block.
        prompt = spawn_args[1]
        self.assertIn("Where does X live?", prompt)
        self.assertIn(
            "We need to know where X lives in the codebase.", prompt,
        )
        self.assertIn("any progress on this?", prompt)
        # The fresh spawn's returned session id is captured for
        # future ticks (already covered by another test, but
        # asserting it here keeps the recovery path self-contained).
        data = gh.pinned_data(55)
        self.assertEqual(data["question_session_id"], "q-sess-fresh")

    def test_resume_persists_new_session_id_from_agent_result(self) -> None:
        # Regression: a prior question tick that yielded no session id
        # (CLI hiccup -- empty codex `-o` file, unparseable claude line)
        # leaves `question_session_id` unset. A later resume that DOES
        # return a session id must persist it, otherwise every future
        # reply re-spawns fresh instead of continuing the locked
        # conversation.
        gh = FakeGitHubClient()
        issue = make_issue(7, label="question")
        issue.comments.append(FakeComment(id=32000, body="follow-up reply"))
        gh.add_issue(issue)
        gh.seed_state(
            7,
            awaiting_human=True,
            last_action_comment_id=31000,
            question_agent=config.DECOMPOSE_AGENT_SPEC,
            # No prior session id captured -- the previous run hiccupped.
            park_reason="question_answer",
        )
        self._run(
            lambda: workflow._handle_question(gh, _TEST_SPEC, issue),
            run_agent=_agent(
                session_id="q-sess-recovered",
                last_message="continued discussion",
            ),
        )
        data = gh.pinned_data(7)
        self.assertEqual(data["question_session_id"], "q-sess-recovered")

    def test_pinned_session_id_is_reused_on_resume(self) -> None:
        # Regression: when the issue already has a persisted spec and
        # session id, the next tick must resume that session rather
        # than spawn a fresh one against the current config.
        gh = FakeGitHubClient()
        issue = make_issue(6, label="question")
        issue.comments.append(FakeComment(id=22000, body="another reply"))
        gh.add_issue(issue)
        gh.seed_state(
            6,
            awaiting_human=True,
            last_action_comment_id=21000,
            question_agent="codex",
            question_session_id="codex-sess-2",
        )
        mocks = self._run(
            lambda: workflow._handle_question(gh, _TEST_SPEC, issue),
            run_agent=_agent(
                session_id="codex-sess-2", last_message="continued",
            ),
        )
        self.assertEqual(
            mocks["run_agent"].call_args.args[0], "codex",
        )
        self.assertEqual(
            mocks["run_agent"].call_args.kwargs.get("resume_session_id"),
            "codex-sess-2",
        )


def _git_env() -> dict:
    """Hermetic git env: detached from the operator's global / system
    config and with a deterministic author/committer so the test does
    not depend on the host's `~/.gitconfig`."""
    return {
        **os.environ,
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_AUTHOR_NAME": "orchestrator-test",
        "GIT_AUTHOR_EMAIL": "orchestrator-test@example.invalid",
        "GIT_COMMITTER_NAME": "orchestrator-test",
        "GIT_COMMITTER_EMAIL": "orchestrator-test@example.invalid",
        "GIT_TERMINAL_PROMPT": "0",
    }


def _run_git(*args: str, cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), check=True,
        capture_output=True, text=True, env=_git_env(),
    )


def _seed_target_root(td: Path) -> tuple[Path, str]:
    """Initialize a temp git repo to serve as `spec.target_root`.

    Creates an initial empty commit on `main` and an `origin/main`
    remote-tracking ref pointing at it, mirroring the shape of a
    freshly-cloned repo just after `_authed_target_fetch`. Returns
    `(target_root, base_sha)` so tests can branch from it.
    """
    target = td / "target"
    target.mkdir()
    _run_git("init", "-q", "-b", "main", cwd=target)
    _run_git("commit", "--allow-empty", "-q", "-m", "init", cwd=target)
    base_sha = _run_git(
        "rev-parse", "HEAD", cwd=target,
    ).stdout.strip()
    _run_git(
        "update-ref",
        "refs/remotes/origin/main", base_sha, cwd=target,
    )
    return target, base_sha


def _spec_for(target_root: Path) -> config.RepoSpec:
    return config.RepoSpec(
        slug="orch/realgit",
        target_root=target_root,
        base_branch="main",
        remote_name="origin",
    )


class BranchHasUnpushedCommitsRealGitTest(unittest.TestCase):
    """Direct coverage for `_branch_has_unpushed_commits`. The stage-
    handler tests mock this helper at the `workflow` facade so they
    do not exercise the real `git rev-list` plumbing; this class
    drives the helper against a real temp-backed clone so a
    regression in the rev-list args, the lock acquisition, or the
    branch-existence pre-check surfaces here.
    """

    def test_returns_false_when_branch_does_not_exist(self) -> None:
        with tempfile.TemporaryDirectory(prefix="bhpc-noBranch-") as td:
            target, _ = _seed_target_root(Path(td))
            spec = _spec_for(target)
            self.assertFalse(
                worktrees._branch_has_unpushed_commits(spec, 700),
            )

    def test_returns_false_when_branch_at_base(self) -> None:
        # `orchestrator/orch__realgit/issue-N` exists at exactly origin/main: a
        # fresh-from-base branch has no commits to inspect.
        with tempfile.TemporaryDirectory(prefix="bhpc-atBase-") as td:
            target, base_sha = _seed_target_root(Path(td))
            _run_git(
                "branch", "orchestrator/orch__realgit/issue-701", base_sha, cwd=target,
            )
            spec = _spec_for(target)
            self.assertFalse(
                worktrees._branch_has_unpushed_commits(spec, 701),
            )

    def test_returns_true_when_branch_has_commits_ahead_of_base(
        self,
    ) -> None:
        # `orchestrator/orch__realgit/issue-N` has at least one commit beyond
        # origin/main. This is the read-only-violation we are
        # trying to detect.
        with tempfile.TemporaryDirectory(prefix="bhpc-ahead-") as td:
            target, base_sha = _seed_target_root(Path(td))
            _run_git(
                "branch", "orchestrator/orch__realgit/issue-702", base_sha, cwd=target,
            )
            # Add a commit on the issue branch. Update the ref
            # directly via `commit-tree` so we don't touch the
            # parent clone's checkout state.
            tree = _run_git(
                "rev-parse", "HEAD^{tree}", cwd=target,
            ).stdout.strip()
            new_commit = _run_git(
                "commit-tree", tree, "-p", base_sha, "-m", "agent commit",
                cwd=target,
            ).stdout.strip()
            _run_git(
                "update-ref", "refs/heads/orchestrator/orch__realgit/issue-702",
                new_commit, cwd=target,
            )
            spec = _spec_for(target)
            self.assertTrue(
                worktrees._branch_has_unpushed_commits(spec, 702),
            )

    def test_returns_false_when_base_remote_ref_missing(self) -> None:
        # If `refs/remotes/origin/main` has been pruned (a
        # mis-configured local clone, a fetch failure earlier in
        # the tick), `git rev-list` exits non-zero. The helper
        # conservatively returns None -- the caller's later steps
        # surface any persistent problem.
        with tempfile.TemporaryDirectory(prefix="bhpc-noBase-") as td:
            target, base_sha = _seed_target_root(Path(td))
            _run_git(
                "branch", "orchestrator/orch__realgit/issue-703", base_sha, cwd=target,
            )
            _run_git(
                "update-ref", "-d",
                "refs/remotes/origin/main", cwd=target,
            )
            spec = _spec_for(target)
            self.assertIsNone(
                worktrees._branch_has_unpushed_commits(spec, 703),
            )

    def test_detects_commits_on_legacy_orchestrator_issue_branch(
        self,
    ) -> None:
        # Regression: a pre-slug-namespacing `question_commits` park
        # holds the question agent's commits on the legacy
        # `orchestrator/issue-N` ref. The pinned state never recorded
        # `branch` (question stage is read-only and never pushed), so
        # the resolver falls back to the slug-namespaced form -- but
        # that branch does not exist locally. Probing ONLY the
        # namespaced form would return None, the `_handle_implementing`
        # relabel guard would clear the park, `_ensure_worktree` would
        # reuse the on-disk worktree (still checked out on the legacy
        # branch), and the recovered-worktree shortcut would push the
        # question-agent commits as a fresh dev PR. The helper must
        # also probe the legacy ref and name it in the return value
        # so the operator hint targets the right branch.
        with tempfile.TemporaryDirectory(prefix="bhpc-legacy-") as td:
            target, base_sha = _seed_target_root(Path(td))
            legacy = "orchestrator/issue-704"
            _run_git("branch", legacy, base_sha, cwd=target)
            tree = _run_git(
                "rev-parse", "HEAD^{tree}", cwd=target,
            ).stdout.strip()
            new_commit = _run_git(
                "commit-tree", tree, "-p", base_sha,
                "-m", "stale question commit",
                cwd=target,
            ).stdout.strip()
            _run_git(
                "update-ref", f"refs/heads/{legacy}", new_commit,
                cwd=target,
            )
            spec = _spec_for(target)
            # Slug-namespaced form does NOT exist; only the legacy
            # form does. Helper must still return the offending
            # branch name (the legacy ref) so the relabel guard fires.
            self.assertEqual(
                worktrees._branch_has_unpushed_commits(spec, 704),
                legacy,
            )

    def test_prefers_namespaced_branch_when_both_exist(self) -> None:
        # Both refs carry commits (a host-restart edge case where the
        # operator force-recreated the namespaced branch without
        # reaping the legacy one). The helper must report the
        # namespaced form first -- that is the branch the rest of the
        # tick will operate on, so it is the one the operator should
        # reset.
        with tempfile.TemporaryDirectory(prefix="bhpc-both-") as td:
            target, base_sha = _seed_target_root(Path(td))
            namespaced = "orchestrator/orch__realgit/issue-705"
            legacy = "orchestrator/issue-705"
            tree = _run_git(
                "rev-parse", "HEAD^{tree}", cwd=target,
            ).stdout.strip()
            for ref in (namespaced, legacy):
                new_commit = _run_git(
                    "commit-tree", tree, "-p", base_sha, "-m", f"c on {ref}",
                    cwd=target,
                ).stdout.strip()
                _run_git(
                    "update-ref", f"refs/heads/{ref}", new_commit,
                    cwd=target,
                )
            spec = _spec_for(target)
            self.assertEqual(
                worktrees._branch_has_unpushed_commits(spec, 705),
                namespaced,
            )


class CleanupQuestionWorktreeRealGitTest(unittest.TestCase):
    """Direct coverage for `_cleanup_question_worktree` against a
    real worktree + local branch. The stage-handler tests mock this
    helper at the `workflow` facade; this class drives the real
    `git worktree remove` + `git branch -D` plumbing so a
    regression in argument order, lock acquisition, or
    error-swallowing surfaces here.
    """

    def _spec_with_worktrees_dir(
        self, target: Path, td: Path,
    ) -> config.RepoSpec:
        # `_worktree_path` is derived from `config.WORKTREES_DIR /
        # sanitized_slug / issue-N`. Patch the module-level config
        # constant for the test so the worktree lands inside `td`
        # and we can cleanly remove the whole directory.
        return _spec_for(target)

    def test_removes_existing_worktree_and_local_branch(self) -> None:
        with tempfile.TemporaryDirectory(prefix="cqw-both-") as td:
            tdp = Path(td)
            target, base_sha = _seed_target_root(tdp)
            # Stand up a worktree at the path `_worktree_path` will
            # compute. Patch WORKTREES_DIR so the slug-derived
            # subdirectory lives inside this temp dir.
            worktrees_dir = tdp / "wts"
            with patch.object(config, "WORKTREES_DIR", worktrees_dir):
                spec = self._spec_with_worktrees_dir(target, tdp)
                expected = worktrees._worktree_path(spec, 800)
                expected.parent.mkdir(parents=True, exist_ok=True)
                _run_git(
                    "worktree", "add", "-b",
                    "orchestrator/orch__realgit/issue-800",
                    str(expected), base_sha, cwd=target,
                )
                self.assertTrue(expected.exists())
                # Branch should exist locally.
                self.assertEqual(
                    0,
                    subprocess.run(
                        ["git", "rev-parse", "--verify", "--quiet",
                         "refs/heads/orchestrator/orch__realgit/issue-800"],
                        cwd=str(target), env=_git_env(),
                        capture_output=True, text=True,
                    ).returncode,
                )

                worktrees._cleanup_question_worktree(spec, 800)

                self.assertFalse(expected.exists())
                # Local branch is gone.
                self.assertNotEqual(
                    0,
                    subprocess.run(
                        ["git", "rev-parse", "--verify", "--quiet",
                         "refs/heads/orchestrator/orch__realgit/issue-800"],
                        cwd=str(target), env=_git_env(),
                        capture_output=True, text=True,
                    ).returncode,
                )

    def test_idempotent_when_nothing_exists(self) -> None:
        # No worktree on disk, no local branch -- the cleanup must
        # not raise (best-effort contract: cleanup never propagates
        # out of the handler).
        with tempfile.TemporaryDirectory(prefix="cqw-nothing-") as td:
            tdp = Path(td)
            target, _ = _seed_target_root(tdp)
            with patch.object(config, "WORKTREES_DIR", tdp / "wts"):
                spec = self._spec_with_worktrees_dir(target, tdp)
                # Should not raise.
                worktrees._cleanup_question_worktree(spec, 801)

    def test_deletes_branch_even_when_worktree_dir_missing(self) -> None:
        # The reviewer's scenario: a prior tick's worktree directory
        # was removed (manual cleanup, or partial cleanup) but the
        # local branch survived. `_cleanup_question_worktree` must
        # still tear the branch down so a later `_ensure_worktree`
        # cannot reuse it.
        with tempfile.TemporaryDirectory(prefix="cqw-branchOnly-") as td:
            tdp = Path(td)
            target, base_sha = _seed_target_root(tdp)
            _run_git(
                "branch", "orchestrator/orch__realgit/issue-802", base_sha, cwd=target,
            )
            with patch.object(config, "WORKTREES_DIR", tdp / "wts"):
                spec = self._spec_with_worktrees_dir(target, tdp)
                # Sanity: worktree path does not exist.
                self.assertFalse(worktrees._worktree_path(spec, 802).exists())

                worktrees._cleanup_question_worktree(spec, 802)

                self.assertNotEqual(
                    0,
                    subprocess.run(
                        ["git", "rev-parse", "--verify", "--quiet",
                         "refs/heads/orchestrator/orch__realgit/issue-802"],
                        cwd=str(target), env=_git_env(),
                        capture_output=True, text=True,
                    ).returncode,
                )


if __name__ == "__main__":
    unittest.main()
