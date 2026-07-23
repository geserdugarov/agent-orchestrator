# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Live `paused` guard for the implementing stage: an operator who applies
`paused` (or `backlog`) WHILE a dev agent run is in flight freezes the issue
before the run's results are published. The guard re-fetches the issue after
the run returns (`gh.get_issue`) rather than trusting the handler's label
snapshot, and on a hit the handler returns without opening a PR, relabeling,
parking, consuming the action watermark, or advancing pinned state -- so once
the label is removed a later tick republishes the committed work normally."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from orchestrator import workflow
from orchestrator.github import PAUSED_LABEL

from tests.fakes import (
    FakeComment,
    FakeGitHubClient,
    FakeLabel,
    FakeUser,
    make_issue,
)
from tests.workflow_helpers import (
    LABEL_IMPLEMENTING,
    _FAKE_WT,
    _PatchedWorkflowMixin,
    _TEST_SPEC,
    _agent,
)

GET_ISSUE = "get_issue"
POISONED_RESUME_ISSUE = 720
RECOVERY_ISSUE = 710
RETRY_ISSUE = 740
HUMAN_REPLY_ID = 1100
ACTION_COMMENT_ID = 900


def _paused_view(number: int) -> object:
    """An `implementing` issue that also carries `paused` -- the state a fresh
    `gh.get_issue` returns after an operator pauses mid-run."""
    view = make_issue(number, label=LABEL_IMPLEMENTING)
    view.labels.append(FakeLabel(PAUSED_LABEL))
    return view


def _assert_fresh_pause_state(
    test_case,
    github,
    before_writes,
    get_issue_mock,
) -> None:
    get_issue_mock.assert_called_with(1)
    test_case.assertEqual(github.opened_prs, [])
    test_case.assertEqual(github.label_history, [])
    test_case.assertEqual(github.posted_comments, [])
    test_case.assertEqual(github.write_state_calls, before_writes)
    pinned_state = github.pinned_data(1)
    test_case.assertNotIn("dev_session_id", pinned_state)
    test_case.assertFalse(pinned_state.get("awaiting_human"))


def _assert_poisoned_pause_state(
    test_case,
    github,
    get_issue_mock,
    before_writes,
    mocks,
) -> None:
    mocks["run_agent"].assert_called_once()
    test_case.assertEqual(get_issue_mock.call_count, 1)
    test_case.assertEqual(github.opened_prs, [])
    test_case.assertEqual(github.label_history, [])
    test_case.assertEqual(github.write_state_calls, before_writes)
    pinned_state = github.pinned_data(POISONED_RESUME_ISSUE)
    test_case.assertEqual(
        pinned_state.get("dev_session_id"),
        "sess-old",
    )
    test_case.assertTrue(pinned_state.get("awaiting_human"))
    test_case.assertEqual(
        pinned_state.get("last_action_comment_id"),
        ACTION_COMMENT_ID,
    )


class ImplementingLivePauseFreshSpawnTest(unittest.TestCase, _PatchedWorkflowMixin):
    def test_fresh_pause_blocks_pr_and_relabel(
        self,
    ) -> None:
        # The handler's `issue` snapshot carries no `paused`; the operator
        # applied it only after the spawn started, so it appears solely on the
        # freshly fetched view. A guard that consulted the stale `issue.labels`
        # would see no hold and open the PR -- asserting no PR proves the guard
        # reads `gh.get_issue`.
        gh = FakeGitHubClient()
        issue = make_issue(1, label=LABEL_IMPLEMENTING)
        gh.add_issue(issue)
        gh.seed_state(
            1,
            user_content_hash=workflow._compute_user_content_hash(issue, set()),
        )
        before_writes = gh.write_state_calls

        get_issue_mock = MagicMock(return_value=_paused_view(1))
        with patch.object(gh, GET_ISSUE, get_issue_mock):
            self._run_implementing(
                gh,
                issue,
                run_agent=_agent(session_id="sess-1", last_message="implemented"),
                # Recovered probe False -> spawn; a True post-agent check would
                # open the PR were the guard absent, so the guard is what keeps
                # `opened_prs` empty.
                has_new_commits=[False, True],
                dirty_files=(),
                push_branch=True,
            )

        _assert_fresh_pause_state(
            self,
            gh,
            before_writes,
            get_issue_mock,
        )


class ImplementingLivePauseResumeTest(unittest.TestCase, _PatchedWorkflowMixin):
    def test_pause_skips_poisoned_retry_refetch(
        self,
    ) -> None:
        # Awaiting-human resume whose first result is a poisoned Claude session
        # ("no conversation found"). Normally `_resume_dev_with_text` would drop
        # the session and immediately retry once as a fresh spawn. With `paused`
        # applied during the run, the guard stops BEFORE that second agent runs
        # -- so `run_agent` fires exactly once -- and before the session id is
        # persisted, `awaiting_human` cleared, or the action watermark consumed.
        #
        # The pause decision is read ONCE, inside the helper, and propagated to
        # the handler -- never re-fetched. The label is deliberately gone on
        # every fetch after the first: a handler that re-read it would see no
        # hold and publish, so `get_issue` being called exactly once (and no PR)
        # proves the observation is propagated, closing that race.
        gh = FakeGitHubClient()
        issue = make_issue(POISONED_RESUME_ISSUE, label=LABEL_IMPLEMENTING)
        reply = FakeComment(
            id=HUMAN_REPLY_ID,
            body="try again please",
            user=FakeUser("alice"),
        )
        issue.comments.append(reply)
        gh.add_issue(issue)
        gh.seed_state(
            POISONED_RESUME_ISSUE,
            awaiting_human=True,
            last_action_comment_id=ACTION_COMMENT_ID,
            dev_agent="claude",
            dev_session_id="sess-old",
            branch=f"orchestrator/geserdugarov__agent-orchestrator/issue-{POISONED_RESUME_ISSUE}",
            user_content_hash=workflow._compute_user_content_hash(issue, set()),
        )
        self._before_writes = gh.write_state_calls

        unpaused_view = make_issue(POISONED_RESUME_ISSUE, label=LABEL_IMPLEMENTING)
        get_issue_mock = MagicMock(
            side_effect=[_paused_view(POISONED_RESUME_ISSUE), unpaused_view, unpaused_view],
        )
        with patch.object(gh, GET_ISSUE, get_issue_mock):
            self._mocks = self._run_implementing(
                gh,
                issue,
                run_agent=_agent(
                    session_id="sess-old",
                    last_message="",
                    stderr="No conversation found with session ID sess-old",
                ),
            )

        _assert_poisoned_pause_state(
            self,
            gh,
            get_issue_mock,
            self._before_writes,
            self._mocks,
        )


class ImplementingLivePauseRecoveryTest(unittest.TestCase, _PatchedWorkflowMixin):
    def test_unpause_republishes_recovered_worktree(self) -> None:
        # End-to-end: tick 1 commits under a live pause and is held; tick 2,
        # after the operator removes `paused`, publishes the stranded commit
        # through the recovered-worktree path and relabels to `validating`.
        gh = FakeGitHubClient()
        issue = make_issue(RECOVERY_ISSUE, label=LABEL_IMPLEMENTING)
        gh.add_issue(issue)
        gh.seed_state(
            RECOVERY_ISSUE,
            user_content_hash=workflow._compute_user_content_hash(issue, set()),
        )

        # Tick 1: fresh spawn commits, but the guard reads the paused view and
        # stops before PR open / relabel, leaving the commit on the branch and
        # pinned state untouched.
        before_writes = gh.write_state_calls
        with patch.object(gh, GET_ISSUE, return_value=_paused_view(RECOVERY_ISSUE)):
            self._run_implementing(
                gh,
                issue,
                run_agent=_agent(session_id="sess-1", last_message="implemented"),
                has_new_commits=[False, True],
                dirty_files=(),
                push_branch=True,
            )
        self.assertEqual(gh.opened_prs, [])
        self.assertEqual(gh.label_history, [])
        self.assertEqual(gh.write_state_calls, before_writes)

        # Tick 2: `paused` removed. `get_issue` now returns the live (unpaused)
        # issue, so the recovered-worktree path skips the agent, publishes, and
        # relabels normally.
        mocks = self._run_implementing(
            gh,
            issue,
            run_agent=_agent(),
            has_new_commits=True,
            dirty_files=(),
            push_branch=True,
        )

        mocks["run_agent"].assert_not_called()
        self.assertEqual(len(gh.opened_prs), 1)
        self.assertIn((RECOVERY_ISSUE, "validating"), gh.label_history)


class ImplementingLivePauseRetryWindowTest(unittest.TestCase, _PatchedWorkflowMixin):
    def test_retry_pause_stops_before_persistence(self) -> None:
        # The first resume trips a poisoned Claude session marker, so the helper
        # drops the session and retries once as a fresh spawn. An operator
        # applies `paused` DURING that retry -- the pre-retry fetch was still
        # clean -- so the second run has its own live-pause window. The guard
        # must re-check after the retry and stop before persisting the fresh
        # session id, clearing `awaiting_human`, or reporting a publishable
        # result.
        gh = FakeGitHubClient()
        issue = make_issue(RETRY_ISSUE, label=LABEL_IMPLEMENTING)
        gh.add_issue(issue)
        gh.seed_state(
            RETRY_ISSUE,
            dev_agent="claude",
            dev_session_id="poisoned-sess",
            awaiting_human=True,
            silent_park_count=0,
        )
        self._state = gh.read_pinned_state(issue)

        self._run_agent = MagicMock(
            side_effect=[
                _agent(
                    session_id="",
                    last_message="",
                    stderr="No conversation found with session ID poisoned-sess",
                ),
                _agent(session_id="fresh-sess", last_message="done"),
            ]
        )

        # First fetch (before the retry) is clean so the first-run guard passes;
        # the second fetch (after the retry) sees the label.
        unpaused = make_issue(RETRY_ISSUE, label=LABEL_IMPLEMENTING)
        get_issue_mock = MagicMock(side_effect=[unpaused, _paused_view(RETRY_ISSUE)])
        with (
            patch.object(gh, GET_ISSUE, get_issue_mock),
            patch.object(workflow, "_ensure_worktree", return_value=_FAKE_WT),
            patch.object(workflow, "run_agent", self._run_agent),
        ):
            _, _, paused = workflow._resume_dev_with_text(
                gh,
                _TEST_SPEC,
                issue,
                self._state,
                "go",
                pause_guard=True,
            )

        # Both runs happened -- the poisoned resume and one bounded fresh retry
        # -- and only then did the guard fire, on the SECOND run's fetch.
        self.assertEqual(
            [agent_call.kwargs.get("resume_session_id") for agent_call in self._run_agent.call_args_list],
            ["poisoned-sess", None],
        )
        self.assertEqual(get_issue_mock.call_count, 2)
        self.assertTrue(paused)
        # The retry's fresh session id is NOT persisted (the poisoned id was
        # dropped and left cleared) and the park is NOT cleared, so the caller
        # returns leaving durable state untouched.
        self.assertIsNone(self._state.get("dev_session_id"))
        self.assertTrue(self._state.get("awaiting_human"))


if __name__ == "__main__":
    unittest.main()
