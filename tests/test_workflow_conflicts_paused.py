# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Live `paused` guard for the resolving_conflict stage across its three dev
resume sites (drift, awaiting-human, fresh conflict resolution): an operator who
applies `paused` (or `backlog`) WHILE the run is in flight freezes the issue
before the run's results are published. `_resume_dev_with_text(pause_guard=True)`
re-fetches the issue after the run returns (`gh.get_issue`) and, on a hit, the
handler returns before `_post_user_content_change_result` /
`_post_conflict_resolution_result` push / relabel / pinned-state writes -- so the
in-progress rebase / resolved commit stays on the branch and the park (if any)
stays intact until the label is removed."""
from __future__ import annotations

import os
import unittest
from unittest.mock import MagicMock, patch

os.environ.setdefault("ORCHESTRATOR_SKIP_DOTENV", "1")

from orchestrator.github import PAUSED_LABEL

from tests.fakes import FakeComment, FakeLabel, FakeUser, make_issue
from tests.workflow_helpers import _ResolvingConflictMixin, _agent


def _paused_view(number: int) -> object:
    view = make_issue(number, label="resolving_conflict")
    view.labels.append(FakeLabel(PAUSED_LABEL))
    return view


class ResolvingConflictLivePauseTest(unittest.TestCase, _ResolvingConflictMixin):
    """A live pause applied mid-run short-circuits each of the three dev
    resume paths before any push / relabel / pinned-state write."""

    def _assert_no_park_comment(self, gh) -> None:
        self.assertFalse(any(
            "timed out" in body
            or "rebase is still in progress" in body
            or "agent needs your input" in body
            or "git push failed" in body
            for _, body in gh.posted_comments
        ))

    def test_paused_during_fresh_resolution_blocks_push_and_relabel(
        self,
    ) -> None:
        # The rebase conflicts, so the dev is spawned to resolve it. The
        # operator applies `paused` mid-run -> the handler returns before
        # `_post_conflict_resolution_result` pushes / increments the round /
        # relabels. The resolved commit stays on the branch, durable state
        # untouched.
        gh, issue, pr = self._seed()
        self._seed_with_baseline_hash(gh, issue)  # quiet drift, no baseline write
        before_writes = gh.write_state_calls

        with patch.object(gh, "get_issue", MagicMock(return_value=_paused_view(200))):
            mocks, merge_mock, _ = self._run_with_merge(
                gh, issue,
                merge_succeeded=False,
                conflicted_files=["a.py"],
                head_shas=["beforehead", "merged"],
                push_branch=True,
            )

        mocks["run_agent"].assert_called_once()
        merge_mock.assert_called_once()  # the rebase ran and conflicted
        mocks["_push_branch"].assert_not_called()
        self.assertEqual(gh.write_state_calls, before_writes)
        self.assertNotIn((200, "validating"), gh.label_history)
        data = gh.pinned_data(200)
        self.assertFalse(data.get("awaiting_human"))
        self.assertEqual(data.get("conflict_round"), 0)
        self._assert_no_park_comment(gh)

    def test_paused_during_awaiting_human_resume_keeps_park(self) -> None:
        # A parked issue resumes the dev on a fresh human reply; the operator
        # applies `paused` mid-run. The handler returns before
        # `_post_conflict_resolution_result`, so the park stays intact and the
        # reply watermark is NOT advanced -- a later tick re-resumes on the
        # same reply once the label is removed.
        gh, issue, pr = self._seed(
            extra_state={
                "awaiting_human": True,
                "conflict_round": 1,
                "last_action_comment_id": 1000,
            },
        )
        issue.comments.append(
            FakeComment(id=2000, body="try harder", user=FakeUser("alice"))
        )
        # Matching hash so the drift path stays quiet and the awaiting-human
        # branch owns the resume (mirrors the interrupted-resume test).
        self._seed_with_baseline_hash(
            gh, issue,
            awaiting_human=True, conflict_round=1, last_action_comment_id=1000,
        )
        before_writes = gh.write_state_calls

        with patch.object(gh, "get_issue", MagicMock(return_value=_paused_view(200))):
            mocks, merge_mock, _ = self._run_with_merge(
                gh, issue,
                merge_succeeded=True,  # unused: the resume path does not rebase
                head_shas=["beforehead", "merged"],
                run_agent_result=_agent(
                    session_id="dev-sess", last_message="resolved",
                ),
            )

        mocks["run_agent"].assert_called_once()
        merge_mock.assert_not_called()
        mocks["_push_branch"].assert_not_called()
        self.assertEqual(gh.write_state_calls, before_writes)
        self.assertNotIn((200, "validating"), gh.label_history)
        data = gh.pinned_data(200)
        self.assertTrue(data.get("awaiting_human"))
        self.assertEqual(data.get("last_action_comment_id"), 1000)
        self.assertEqual(data.get("conflict_round"), 1)

    def test_paused_during_drift_resume_blocks_relabel(self) -> None:
        # A body edit drives the drift resume (seeded hash mismatch); the
        # operator applies `paused` mid-run. The handler returns before
        # `_post_user_content_change_result` and the conflict-round bump, so
        # the drift stays unconsumed (the stale hash stands) and no relabel /
        # write happens.
        gh, issue, pr = self._seed(
            extra_state={"user_content_hash": "stale-hash"},
        )
        before_writes = gh.write_state_calls

        with patch.object(gh, "get_issue", MagicMock(return_value=_paused_view(200))):
            mocks, merge_mock, _ = self._run_with_merge(
                gh, issue,
                merge_succeeded=True,  # unused: drift returns before the rebase
                head_shas=["beforehead", "merged"],
                run_agent_result=_agent(
                    session_id="dev-sess", last_message="addressed",
                ),
            )

        mocks["run_agent"].assert_called_once()
        merge_mock.assert_not_called()  # drift returns before the base rebase
        mocks["_push_branch"].assert_not_called()
        self.assertEqual(gh.write_state_calls, before_writes)
        self.assertNotIn((200, "validating"), gh.label_history)
        data = gh.pinned_data(200)
        self.assertEqual(data.get("user_content_hash"), "stale-hash")
        self.assertFalse(data.get("awaiting_human"))


if __name__ == "__main__":
    unittest.main()
