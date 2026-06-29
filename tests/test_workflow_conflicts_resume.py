# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import os
import unittest
from unittest.mock import MagicMock, patch

os.environ.setdefault("ORCHESTRATOR_SKIP_DOTENV", "1")

from orchestrator import workflow

from tests.fakes import FakeComment, FakeUser
from tests.workflow_helpers import (
    _FAKE_WT,
    _ResolvingConflictMixin,
    _TEST_SPEC,
    _agent,
)


class ResolvingConflictAwaitingHumanResumeTest(
    unittest.TestCase, _ResolvingConflictMixin
):
    """Drive `_handle_resolving_conflict` through the awaiting-human resume
    branches: a parked issue stays quiet without a fresh reply, resumes the
    dev on a new comment, re-parks on a follow-up question, recovers from a
    stale Claude session, and discards an interrupted resume.
    """

    def test_awaiting_human_no_new_comments_is_quiet(self) -> None:
        # Once parked, ticks without a new human reply must not retry --
        # otherwise the cap is meaningless and a poisoned rebase would
        # burn tokens. The parked state stays put.
        gh, issue, pr = self._seed(
            extra_state={
                "awaiting_human": True,
                "conflict_round": 1,
                # Watermark above any comment so `comments_after` is empty.
                "last_action_comment_id": 999_999,
            },
        )
        merge_mock = MagicMock(return_value=(True, []))
        git_mock = MagicMock(
            return_value=MagicMock(returncode=0, stdout="", stderr="")
        )
        with patch.object(
            workflow, "_rebase_base_into_worktree", merge_mock
        ), patch.object(workflow, "_git", git_mock), patch.object(
            workflow, "_git_hardened", git_mock,
        ):
            mocks = self._run(
                lambda: workflow._handle_resolving_conflict(
                    gh, _TEST_SPEC, issue,
                ),
                run_agent=_agent(),
                push_branch=True,
            )
        merge_mock.assert_not_called()
        mocks["run_agent"].assert_not_called()
        self.assertEqual(gh.label_history, [])

    def test_awaiting_human_with_new_comment_resumes_dev(self) -> None:
        # `_on_question` / `_on_dirty_worktree` parks tell the human
        # "reply with guidance and the orchestrator will resume the
        # session". Honor that contract: a fresh comment past the
        # watermark must resume the dev on the in-progress rebase
        # worktree, NOT keep the issue stuck until a manual relabel.
        gh, issue, pr = self._seed(
            extra_state={
                "awaiting_human": True,
                "conflict_round": 1,
                "last_action_comment_id": 1000,
            },
        )
        # Fresh comment above the watermark.
        issue.comments.append(
            FakeComment(
                id=2000, body="try harder; conflict in foo.py is structural",
                user=FakeUser("alice"),
            )
        )

        mocks, merge_mock, _ = self._run_with_merge(
            gh, issue,
            merge_succeeded=True,  # unused on resume path
            head_shas=["beforehead", "merged"],
            push_branch=True,
        )

        # Resume runs the agent with the human's text; rebase is NOT
        # re-attempted (the worktree is mid-rebase already).
        mocks["run_agent"].assert_called_once()
        prompt = mocks["run_agent"].call_args.args[1]
        self.assertIn("try harder", prompt)
        # The bare human-reply followup must carry the foreground-only
        # execution-model note -- a resumed dev that backgrounds a slow
        # test run and ends its turn "to check later" strands the issue
        # (the job dies with the session).
        self.assertIn("NEVER start a background job", prompt)
        merge_mock.assert_not_called()
        # Successful resume pushes the branch and hands straight back
        # to `validating`. Docs do not run here -- the single docs pass
        # runs after reviewer approval before `in_review` via the
        # final-docs handoff.
        mocks["_push_branch"].assert_called_once_with(
            _TEST_SPEC,
            _FAKE_WT,
            self.BRANCH,
            force_with_lease=None,
        )
        data = gh.pinned_data(200)
        self.assertEqual(data.get("review_round"), 0)
        self.assertEqual(data.get("conflict_round"), 2)
        self.assertIn((200, "validating"), gh.label_history)
        self.assertNotIn((200, "documenting"), gh.label_history)
        # Watermark advanced past the consumed comment.
        self.assertEqual(data.get("last_action_comment_id"), 2000)

    def test_awaiting_human_resume_interrupted_does_not_consume_reply(
        self,
    ) -> None:
        gh, issue, pr = self._seed(
            extra_state={
                "awaiting_human": True,
                "conflict_round": 1,
                "last_action_comment_id": 1000,
            },
        )
        # Fresh comment above the watermark drives the resume.
        issue.comments.append(
            FakeComment(
                id=2000, body="try the three-way merge",
                user=FakeUser("alice"),
            )
        )
        # Seed the hash AFTER the comment so drift stays quiet and the
        # awaiting-human branch (not the drift path) owns the resume.
        self._seed_with_baseline_hash(
            gh, issue,
            awaiting_human=True, conflict_round=1, last_action_comment_id=1000,
        )
        before_writes = gh.write_state_calls

        mocks, merge_mock, _ = self._run_with_merge(
            gh, issue,
            merge_succeeded=True,  # unused on the resume path
            head_shas=["beforehead", "merged"],
            run_agent_result=_agent(
                session_id="dev-sess", last_message="", interrupted=True,
            ),
        )

        mocks["run_agent"].assert_called_once()
        merge_mock.assert_not_called()
        self.assertEqual(gh.write_state_calls, before_writes)
        data = gh.pinned_data(200)
        # Park not consumed, reply watermark not advanced -- the next process
        # re-resumes on the same comment.
        self.assertTrue(data.get("awaiting_human"))
        self.assertEqual(data.get("last_action_comment_id"), 1000)
        self.assertEqual(data.get("conflict_round"), 1)
        self.assertNotIn((200, "validating"), gh.label_history)

    def test_awaiting_human_resume_recovers_from_stale_claude_session(self) -> None:
        # Regression: a `resolving_conflict` issue parked awaiting human
        # whose pinned `dev_session_id` references a Claude transcript that
        # no longer exists. The first `--resume <sid>` call comes back with
        # `No conversation found with session ID` on stderr and empty
        # stdout. Without immediate detection the resume would surface as
        # an `agent_silent` park, the silent-park counter would tick to 1
        # (still below the threshold), and the human would have to comment
        # twice more before recovery. With the fix, `_resume_dev_with_text`
        # transparently retries with a fresh spawn in the same worktree;
        # the rebase commit produced by the retry pushes and the issue
        # flips back to validating in a single tick.
        gh, issue, pr = self._seed(
            extra_state={
                "awaiting_human": True,
                "conflict_round": 1,
                "last_action_comment_id": 1000,
                "dev_session_id": "poisoned-sess",
            },
        )
        issue.comments.append(
            FakeComment(
                id=2000, body="please retry the conflict resolution",
                user=FakeUser("alice"),
            )
        )

        stale_stderr = "Error: No conversation found with session ID: poisoned-sess"

        calls: list = []

        def fake_run(agent, prompt, wt, *, resume_session_id=None, extra_args=()):
            calls.append(resume_session_id)
            if resume_session_id == "poisoned-sess":
                return _agent(
                    session_id="", last_message="", stderr=stale_stderr,
                )
            return _agent(session_id="fresh-sess", last_message="resolved")

        merge_mock = MagicMock(return_value=(True, []))
        git_mock = MagicMock(
            return_value=MagicMock(returncode=0, stdout="", stderr="")
        )
        with patch.object(
            workflow, "_rebase_base_into_worktree", merge_mock,
        ), patch.object(workflow, "_git", git_mock), patch.object(
            workflow, "_git_hardened", git_mock,
        ):
            mocks = self._run(
                lambda: workflow._handle_resolving_conflict(
                    gh, _TEST_SPEC, issue,
                ),
                run_agent=fake_run,
                push_branch=True,
                head_shas=["beforehead", "merged"],
            )

        # Two run_agent calls: the poisoned resume + the fresh-spawn retry.
        self.assertEqual(
            calls, ["poisoned-sess", None],
            "stale-session resume must be transparently retried as fresh",
        )
        # Successful retry pushes the branch and hands straight back to
        # `validating` WITHOUT parking agent_silent; the single docs
        # pass is deferred to the post-approval hop.
        mocks["_push_branch"].assert_called_once()
        self.assertIn((200, "validating"), gh.label_history)
        self.assertNotIn((200, "documenting"), gh.label_history)
        data = gh.pinned_data(200)
        self.assertFalse(
            data.get("awaiting_human"),
            "awaiting_human must be cleared on a recovered resume",
        )
        self.assertNotEqual(data.get("park_reason"), "agent_silent")
        self.assertEqual(data.get("conflict_round"), 2)
        self.assertEqual(data.get("dev_session_id"), "fresh-sess")

    def test_awaiting_human_resume_with_question_parks_again(self) -> None:
        # Resumed agent that produces no new commit (asks another
        # question) must re-park rather than push or flip the label.
        gh, issue, pr = self._seed(
            extra_state={
                "awaiting_human": True,
                "conflict_round": 1,
                "last_action_comment_id": 1000,
            },
        )
        issue.comments.append(
            FakeComment(
                id=2000, body="try harder",
                user=FakeUser("alice"),
            )
        )

        mocks, merge_mock, _ = self._run_with_merge(
            gh, issue,
            merge_succeeded=True,
            # Same SHA before and after -- agent did nothing.
            head_shas=["samehead", "samehead"],
            push_branch=True,
            run_agent_result=_agent(
                session_id="dev-sess",
                last_message="I still need clarification on bar.py",
            ),
        )

        mocks["run_agent"].assert_called_once()
        merge_mock.assert_not_called()
        mocks["_push_branch"].assert_not_called()
        data = gh.pinned_data(200)
        # Re-parked: counter unchanged, no label flip.
        self.assertEqual(data.get("conflict_round"), 1)
        self.assertNotIn((200, "validating"), gh.label_history)
        self.assertTrue(data.get("awaiting_human"))


if __name__ == "__main__":
    unittest.main()
