# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Agent lifecycle audit event tests."""
from __future__ import annotations

import unittest
from unittest.mock import patch

from orchestrator import config, workflow

from tests import workflow_event_emission_test_support as support


class AgentLifecycleEventEmissionTest(unittest.TestCase, support._PatchedWorkflowMixin):
    """`_run_agent_tracked` bookends every agent invocation with
    `agent_spawn` / `agent_exit` events carrying the role, stage, session
    id, duration, and timeout/exit metadata. Optional context fields
    (review_round, retry_count) are recorded when present.

    These tests exercise the in-memory `recorded_events` capture on the
    fake; the same records are written to disk when EVENT_LOG_PATH is set
    (the StageEventEmissionTest covers the on-disk surface).
    """

    def test_fresh_dev_spawn_emits_event_pair(self) -> None:
        gh = support.FakeGitHubClient()
        issue = support.make_issue(1, label=support.LABEL_IMPLEMENTING)
        gh.add_issue(issue)
        self._run(
            lambda: workflow._handle_implementing(gh, support._TEST_SPEC, issue),
            run_agent=support._agent(session_id="sess-dev", last_message="q?"),
            has_new_commits=False,
        )
        spawn = support._only_role_event(
            gh,
            support.EVENT_AGENT_SPAWN,
            support.ROLE_DEVELOPER,
        )
        exit_event = support._only_role_event(
            gh,
            support.EVENT_AGENT_EXIT,
            support.ROLE_DEVELOPER,
        )
        self.assertEqual(
            (
                spawn[support._STAGE_KEY],
                spawn[support._AGENT_ROLE_KEY],
                spawn["agent"],
                support._SESSION_ID_KEY in spawn,
                exit_event[support._SESSION_ID_KEY],
                exit_event["exit_code"],
                exit_event["timed_out"],
                "duration_s" in exit_event,
            ),
            (
                support.LABEL_IMPLEMENTING,
                support.ROLE_DEVELOPER,
                config.DEV_AGENT,
                False,
                "sess-dev",
                0,
                False,
                True,
            ),
        )
        self.assertGreaterEqual(exit_event["duration_s"], 0)
        # retry_count is incremented to 1 by `_check_and_increment_retry_budget`
        # BEFORE the spawn, so the recorded value is what the agent ran under.
        self.assertEqual(
            (
                spawn[support._RETRY_COUNT_KEY],
                exit_event[support._RETRY_COUNT_KEY],
            ),
            (1, 1),
        )

    def test_reviewer_spawn_has_round_and_retry(self) -> None:
        gh = support.FakeGitHubClient()
        issue = support.make_issue(2, label=support.LABEL_VALIDATING)
        gh.add_issue(issue)
        pr = support.FakePR(
            number=support._REVIEW_PR_NUMBER,
            head_branch="orchestrator/geserdugarov__agent-orchestrator/issue-2",
            base_branch=support.TEST_BASE_BRANCH,
            mergeable=True,
            check_state="success",
            approved=False,
        )
        gh.add_pr(pr)
        # Seed both `review_round` and `retry_count` so both optional
        # context fields ride along on the reviewer's spawn/exit events.
        gh.seed_state(
            2,
            pr_number=support._REVIEW_PR_NUMBER,
            review_round=1,
            retry_count=2,
        )
        # Patch _latest_pr_comment_ids so it doesn't touch real GitHub.
        with patch.object(
            workflow, "_latest_pr_comment_ids", return_value=(None, None)
        ):
            self._run(
                lambda: workflow._handle_validating(gh, support._TEST_SPEC, issue),
                run_agent=support._agent(
                    session_id="sess-review",
                    last_message=support.REVIEW_APPROVED_MESSAGE,
                ),
                head_shas=[pr.head.sha, pr.head.sha],
            )
        reviewer_spawn = support._only_role_event(
            gh,
            support.EVENT_AGENT_SPAWN,
            support.ROLE_REVIEWER,
        )
        reviewer_exit = support._only_role_event(
            gh,
            support.EVENT_AGENT_EXIT,
            support.ROLE_REVIEWER,
        )
        self.assertEqual(
            (
                reviewer_spawn[support._STAGE_KEY],
                reviewer_spawn["agent"],
                reviewer_spawn["review_round"],
                reviewer_spawn[support._RETRY_COUNT_KEY],
                reviewer_exit["review_round"],
                reviewer_exit[support._RETRY_COUNT_KEY],
                reviewer_exit[support._SESSION_ID_KEY],
            ),
            (
                support.LABEL_VALIDATING,
                config.REVIEW_AGENT,
                1,
                2,
                1,
                2,
                "sess-review",
            ),
        )

    def test_dev_resume_spawn_carries_session_id(self) -> None:
        # A resume hands the spawn event the existing session id; the exit
        # event records the (same) live id from the AgentResult.
        gh = support.FakeGitHubClient()
        issue = support.make_issue(3, label=support.LABEL_IMPLEMENTING)
        issue.comments.append(
            support.FakeComment(id=support._RESUME_COMMENT_ID, body="please retry"),
        )
        gh.add_issue(issue)
        gh.seed_state(
            3,
            awaiting_human=True,
            last_action_comment_id=support._LAST_ACTION_COMMENT_ID,
            dev_agent="codex", dev_session_id="sess-resume",
        )
        self._run(
            lambda: workflow._handle_implementing(gh, support._TEST_SPEC, issue),
            run_agent=support._agent(session_id="sess-resume", last_message="q?"),
            has_new_commits=False,
        )
        spawns = support._events(gh, support.EVENT_AGENT_SPAWN)
        self.assertEqual(len(spawns), 1)
        self.assertEqual(spawns[0][support._AGENT_ROLE_KEY], support.ROLE_DEVELOPER)
        self.assertEqual(spawns[0][support._SESSION_ID_KEY], "sess-resume")

    def test_timeout_records_timed_out_flag_on_exit(self) -> None:
        gh = support.FakeGitHubClient()
        issue = support.make_issue(4, label=support.LABEL_IMPLEMENTING)
        gh.add_issue(issue)
        self._run(
            lambda: workflow._handle_implementing(gh, support._TEST_SPEC, issue),
            run_agent=support._agent(timed_out=True, last_message=""),
            has_new_commits=False,
            # before_sha == after_sha: the timeout produced no new commit, so
            # the issue parks (the disposition reads HEAD twice now).
            head_shas=("sha-pre", "sha-pre"),
        )
        exits = support._events(gh, support.EVENT_AGENT_EXIT)
        self.assertEqual(len(exits), 1)
        self.assertTrue(exits[0]["timed_out"])
        self.assertEqual(exits[0]["exit_code"], -1)
