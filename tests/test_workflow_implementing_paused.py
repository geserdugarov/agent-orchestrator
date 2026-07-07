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

import os
import unittest
from unittest.mock import MagicMock, patch

os.environ.setdefault("ORCHESTRATOR_SKIP_DOTENV", "1")

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
    _FAKE_WT,
    _PatchedWorkflowMixin,
    _TEST_SPEC,
    _agent,
)


def _paused_view(number: int) -> object:
    """An `implementing` issue that also carries `paused` -- the state a fresh
    `gh.get_issue` returns after an operator pauses mid-run."""
    view = make_issue(number, label="implementing")
    view.labels.append(FakeLabel(PAUSED_LABEL))
    return view


class ImplementingLivePauseFreshSpawnTest(unittest.TestCase, _PatchedWorkflowMixin):
    def test_paused_during_run_blocks_pr_and_relabel_reading_fresh_issue(
        self,
    ) -> None:
        # The handler's `issue` snapshot carries no `paused`; the operator
        # applied it only after the spawn started, so it appears solely on the
        # freshly fetched view. A guard that consulted the stale `issue.labels`
        # would see no hold and open the PR -- asserting no PR proves the guard
        # reads `gh.get_issue`.
        gh = FakeGitHubClient()
        issue = make_issue(1, label="implementing")
        gh.add_issue(issue)
        gh.seed_state(
            1, user_content_hash=workflow._compute_user_content_hash(issue, set()),
        )
        before_writes = gh.write_state_calls

        get_issue_mock = MagicMock(return_value=_paused_view(1))
        with patch.object(gh, "get_issue", get_issue_mock):
            self._run(
                lambda: workflow._handle_implementing(gh, _TEST_SPEC, issue),
                run_agent=_agent(session_id="sess-1", last_message="implemented"),
                # Recovered probe False -> spawn; a True post-agent check would
                # open the PR were the guard absent, so the guard is what keeps
                # `opened_prs` empty.
                has_new_commits=[False, True],
                dirty_files=(),
                push_branch=True,
            )

        get_issue_mock.assert_called_with(1)
        # No publish, no relabel, no HITL park comment.
        self.assertEqual(gh.opened_prs, [])
        self.assertEqual(gh.label_history, [])
        self.assertEqual(gh.posted_comments, [])
        # Durable state untouched: the fresh session id is discarded and no
        # pinned-state advancement is written, so the next tick resumes intact.
        self.assertEqual(gh.write_state_calls, before_writes)
        state = gh.pinned_data(1)
        self.assertNotIn("dev_session_id", state)
        self.assertFalse(state.get("awaiting_human"))


class ImplementingLivePauseResumeTest(unittest.TestCase, _PatchedWorkflowMixin):
    def test_paused_during_resume_skips_poisoned_retry_and_is_not_refetched(
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
        issue = make_issue(720, label="implementing")
        issue.comments.append(
            FakeComment(id=1100, body="try again please", user=FakeUser("alice"))
        )
        gh.add_issue(issue)
        gh.seed_state(
            720,
            awaiting_human=True,
            last_action_comment_id=900,
            dev_agent="claude",
            dev_session_id="sess-old",
            branch="orchestrator/geserdugarov__agent-orchestrator/issue-720",
            user_content_hash=workflow._compute_user_content_hash(issue, set()),
        )
        before_writes = gh.write_state_calls

        unpaused = make_issue(720, label="implementing")
        get_issue_mock = MagicMock(
            side_effect=[_paused_view(720), unpaused, unpaused],
        )
        with patch.object(gh, "get_issue", get_issue_mock):
            mocks = self._run(
                lambda: workflow._handle_implementing(gh, _TEST_SPEC, issue),
                run_agent=_agent(
                    session_id="sess-old",
                    last_message="",
                    stderr="No conversation found with session ID sess-old",
                ),
            )

        # The poisoned-session retry -- a second agent spawn -- never fired.
        mocks["run_agent"].assert_called_once()
        # The label was read exactly once (the helper's fetch); the handler
        # honored that decision instead of re-reading the now-unpaused issue.
        self.assertEqual(get_issue_mock.call_count, 1)
        self.assertEqual(gh.opened_prs, [])
        self.assertEqual(gh.label_history, [])
        # No pinned-state advancement: the session id, park flag, and action
        # watermark all stay exactly as the prior tick left them.
        self.assertEqual(gh.write_state_calls, before_writes)
        state = gh.pinned_data(720)
        self.assertEqual(state.get("dev_session_id"), "sess-old")
        self.assertTrue(state.get("awaiting_human"))
        self.assertEqual(state.get("last_action_comment_id"), 900)


class ImplementingLivePauseRecoveryTest(unittest.TestCase, _PatchedWorkflowMixin):
    def test_paused_then_removed_republishes_via_recovered_worktree(self) -> None:
        # End-to-end: tick 1 commits under a live pause and is held; tick 2,
        # after the operator removes `paused`, publishes the stranded commit
        # through the recovered-worktree path and relabels to `validating`.
        gh = FakeGitHubClient()
        issue = make_issue(710, label="implementing")
        gh.add_issue(issue)
        gh.seed_state(
            710, user_content_hash=workflow._compute_user_content_hash(issue, set()),
        )

        # Tick 1: fresh spawn commits, but the guard reads the paused view and
        # stops before PR open / relabel, leaving the commit on the branch and
        # pinned state untouched.
        before_writes = gh.write_state_calls
        with patch.object(gh, "get_issue", return_value=_paused_view(710)):
            self._run(
                lambda: workflow._handle_implementing(gh, _TEST_SPEC, issue),
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
        mocks = self._run(
            lambda: workflow._handle_implementing(gh, _TEST_SPEC, issue),
            run_agent=_agent(),
            has_new_commits=True,
            dirty_files=(),
            push_branch=True,
        )

        mocks["run_agent"].assert_not_called()
        self.assertEqual(len(gh.opened_prs), 1)
        self.assertIn((710, "validating"), gh.label_history)


class ImplementingLivePauseRetryWindowTest(
    unittest.TestCase, _PatchedWorkflowMixin
):
    def test_pause_during_poisoned_retry_stops_before_persistence(self) -> None:
        # The first resume trips a poisoned Claude session marker, so the helper
        # drops the session and retries once as a fresh spawn. An operator
        # applies `paused` DURING that retry -- the pre-retry fetch was still
        # clean -- so the second run has its own live-pause window. The guard
        # must re-check after the retry and stop before persisting the fresh
        # session id, clearing `awaiting_human`, or reporting a publishable
        # result.
        gh = FakeGitHubClient()
        issue = make_issue(740, label="implementing")
        gh.add_issue(issue)
        gh.seed_state(
            740,
            dev_agent="claude",
            dev_session_id="poisoned-sess",
            awaiting_human=True,
            silent_park_count=0,
        )
        state = gh.read_pinned_state(issue)

        calls: list = []

        def fake_run(agent, prompt, wt, *, resume_session_id=None, extra_args=()):
            calls.append(resume_session_id)
            if resume_session_id == "poisoned-sess":
                return _agent(
                    session_id="",
                    last_message="",
                    stderr="No conversation found with session ID poisoned-sess",
                )
            return _agent(session_id="fresh-sess", last_message="done")

        # First fetch (before the retry) is clean so the first-run guard passes;
        # the second fetch (after the retry) sees the label.
        unpaused = make_issue(740, label="implementing")
        get_issue_mock = MagicMock(side_effect=[unpaused, _paused_view(740)])
        with patch.object(gh, "get_issue", get_issue_mock), \
             patch.object(
                 workflow, "_ensure_worktree", lambda spec, n, **_: _FAKE_WT,
             ), \
             patch.object(workflow, "run_agent", fake_run):
            _, _, paused = workflow._resume_dev_with_text(
                gh, _TEST_SPEC, issue, state, "go", pause_guard=True,
            )

        # Both runs happened -- the poisoned resume and one bounded fresh retry
        # -- and only then did the guard fire, on the SECOND run's fetch.
        self.assertEqual(calls, ["poisoned-sess", None])
        self.assertEqual(get_issue_mock.call_count, 2)
        self.assertTrue(paused)
        # The retry's fresh session id is NOT persisted (the poisoned id was
        # dropped and left cleared) and the park is NOT cleared, so the caller
        # returns leaving durable state untouched.
        self.assertIsNone(state.get("dev_session_id"))
        self.assertTrue(state.get("awaiting_human"))


if __name__ == "__main__":
    unittest.main()
