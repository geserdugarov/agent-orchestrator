# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Audit-event emission from `set_workflow_label` and `_run_agent_tracked`:
one `stage_enter` per label flip, paired `agent_spawn`/`agent_exit` per
agent run, optional `session_id`/`review_round`/`retry_count` context, and
the JSONL sink driven by `EVENT_LOG_PATH`."""
from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from orchestrator import config, workflow

from tests.fakes import (
    FakeComment,
    FakeGitHubClient,
    FakePR,
    make_issue,
)
from tests.workflow_helpers import (
    EVENT_AGENT_EXIT,
    EVENT_AGENT_SPAWN,
    EVENT_STAGE_ENTER,
    LABEL_DECOMPOSING,
    LABEL_DOCUMENTING,
    LABEL_IMPLEMENTING,
    LABEL_VALIDATING,
    REVIEW_APPROVED_MESSAGE,
    ROLE_DEVELOPER,
    ROLE_REVIEWER,
    TEST_BASE_BRANCH,
    TEST_REPO_SLUG,
    _PatchedWorkflowMixin,
    _TEST_SPEC,
    _agent,
)


_EVENT_KEY = "event"
_STAGE_KEY = "stage"
_AGENT_ROLE_KEY = "agent_role"
_SESSION_ID_KEY = "session_id"
_RETRY_COUNT_KEY = "retry_count"
_REVIEW_PR_NUMBER = 42
_RESUME_COMMENT_ID = 2000
_LAST_ACTION_COMMENT_ID = 1500


def _events(gh: FakeGitHubClient, event_name: str) -> list[dict]:
    return [
        event for event in gh.recorded_events
        if event[_EVENT_KEY] == event_name
    ]


class StageEventEmissionTest(unittest.TestCase, _PatchedWorkflowMixin):
    """`set_workflow_label` is the single chokepoint for stage transitions,
    so a hook there gives every workflow handler a `stage_enter` event for
    free. The fake mirrors the real client's `recorded_events` capture and
    JSONL sink so workflow tests can assert on either surface.
    """

    def test_label_change_records_stage_enter(self) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(1)
        gh.add_issue(issue)
        gh.set_workflow_label(issue, LABEL_IMPLEMENTING)
        self.assertEqual(len(gh.recorded_events), 1)
        event = gh.recorded_events[0]
        self.assertEqual(event[_EVENT_KEY], EVENT_STAGE_ENTER)
        self.assertEqual(event[_STAGE_KEY], LABEL_IMPLEMENTING)
        self.assertEqual(event["issue"], 1)
        self.assertEqual(event["repo"], TEST_REPO_SLUG)
        self.assertIn("ts", event)
        # UTC timestamp, ISO 8601 with offset.
        datetime.fromisoformat(event["ts"])

    def test_none_label_does_not_emit(self) -> None:
        # Clearing the workflow label is not a stage; the helper must
        # short-circuit so downstream consumers don't see a phantom
        # `stage_enter` with stage=None.
        gh = FakeGitHubClient()
        issue = make_issue(1, label=LABEL_IMPLEMENTING)
        gh.add_issue(issue)
        gh.set_workflow_label(issue, None)
        self.assertEqual(gh.recorded_events, [])

    def test_pickup_emits_decomposing_stage_enter(self) -> None:
        # The hook is centralized: a real handler call (no manual label
        # flip in the test) still produces the event because
        # `_handle_pickup` routes through `gh.set_workflow_label`.
        gh = FakeGitHubClient()
        issue = make_issue(1)
        gh.add_issue(issue)
        with patch.object(config, "DECOMPOSE", True):
            self._run(
                lambda: workflow._handle_pickup(gh, _TEST_SPEC, issue),
                run_agent=_agent(last_message="need clarification"),
                has_new_commits=False,
            )
        stages = [
            event[_STAGE_KEY] for event in gh.recorded_events
            if event[_EVENT_KEY] == EVENT_STAGE_ENTER
        ]
        self.assertIn(LABEL_DECOMPOSING, stages)

    def test_event_log_writes_one_object_per_line(self) -> None:
        # End-to-end: a configured EVENT_LOG_PATH receives one parseable
        # JSONL object per transition, with the documented schema.
        with tempfile.TemporaryDirectory(prefix="evlog-") as td:
            path = Path(td) / "events.jsonl"
            with patch.object(config, "EVENT_LOG_PATH", path):
                gh = FakeGitHubClient()
                issue = make_issue(7)
                gh.add_issue(issue)
                # A legal forward path (implementing -> validating ->
                # documenting) so the sequence emits three stage_enter events
                # without tripping the transition guard under `enforce`.
                gh.set_workflow_label(issue, LABEL_IMPLEMENTING)
                gh.set_workflow_label(issue, LABEL_VALIDATING)
                gh.set_workflow_label(issue, LABEL_DOCUMENTING)
            # File closed on context exit -- read it back, parse line by line.
            lines = path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 3)
            records = [json.loads(line) for line in lines]
            self.assertEqual(
                [record[_STAGE_KEY] for record in records],
                [LABEL_IMPLEMENTING, LABEL_VALIDATING, LABEL_DOCUMENTING],
            )
            for record in records:
                self.assertEqual(record[_EVENT_KEY], EVENT_STAGE_ENTER)
                self.assertEqual(record["issue"], 7)
                self.assertEqual(record["repo"], TEST_REPO_SLUG)
                # ts must be a valid ISO-8601 UTC timestamp.
                ts = datetime.fromisoformat(record["ts"])
                self.assertEqual(ts.tzinfo, timezone.utc)
            # JSONL invariant: exactly one object per line, no blank lines.
            for line in lines:
                self.assertTrue(line.strip())
                self.assertFalse(line.startswith(" "))

    def test_event_log_path_unset_writes_no_file(self) -> None:
        # The legacy behavior is that no event file exists; flipping a
        # label must not create one when EVENT_LOG_PATH is unset.
        with tempfile.TemporaryDirectory(prefix="evlog-off-") as td:
            sentinel = Path(td) / "should-not-be-created.jsonl"
            with patch.object(config, "EVENT_LOG_PATH", None):
                gh = FakeGitHubClient()
                issue = make_issue(1)
                gh.add_issue(issue)
                gh.set_workflow_label(issue, LABEL_IMPLEMENTING)
            self.assertFalse(sentinel.exists())
            # In-memory capture still works even with the file sink disabled,
            # so tests don't need a temp file to inspect transitions.
            self.assertEqual(len(gh.recorded_events), 1)


class AgentLifecycleEventEmissionTest(unittest.TestCase, _PatchedWorkflowMixin):
    """`_run_agent_tracked` bookends every agent invocation with
    `agent_spawn` / `agent_exit` events carrying the role, stage, session
    id, duration, and timeout/exit metadata. Optional context fields
    (review_round, retry_count) are recorded when present.

    These tests exercise the in-memory `recorded_events` capture on the
    fake; the same records are written to disk when EVENT_LOG_PATH is set
    (the StageEventEmissionTest covers the on-disk surface).
    """

    def test_fresh_dev_spawn_emits_event_pair(self) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(1, label=LABEL_IMPLEMENTING)
        gh.add_issue(issue)
        self._run(
            lambda: workflow._handle_implementing(gh, _TEST_SPEC, issue),
            run_agent=_agent(session_id="sess-dev", last_message="q?"),
            has_new_commits=False,
        )
        spawns = _events(gh, EVENT_AGENT_SPAWN)
        exits = _events(gh, EVENT_AGENT_EXIT)
        self.assertEqual(len(spawns), 1)
        self.assertEqual(len(exits), 1)
        spawn = spawns[0]
        exit_event = exits[0]
        self.assertEqual(spawn[_STAGE_KEY], LABEL_IMPLEMENTING)
        self.assertEqual(spawn[_AGENT_ROLE_KEY], ROLE_DEVELOPER)
        self.assertEqual(spawn["agent"], config.DEV_AGENT)
        self.assertNotIn(_SESSION_ID_KEY, spawn)  # fresh spawn -- no resume id
        self.assertEqual(exit_event[_SESSION_ID_KEY], "sess-dev")
        self.assertEqual(exit_event["exit_code"], 0)
        self.assertFalse(exit_event["timed_out"])
        self.assertIn("duration_s", exit_event)
        self.assertGreaterEqual(exit_event["duration_s"], 0)
        # retry_count is incremented to 1 by `_check_and_increment_retry_budget`
        # BEFORE the spawn, so the recorded value is what the agent ran under.
        self.assertEqual(spawn[_RETRY_COUNT_KEY], 1)
        self.assertEqual(exit_event[_RETRY_COUNT_KEY], 1)

    def test_reviewer_spawn_has_round_and_retry(self) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(2, label=LABEL_VALIDATING)
        gh.add_issue(issue)
        pr = FakePR(
            number=_REVIEW_PR_NUMBER,
            head_branch="orchestrator/geserdugarov__agent-orchestrator/issue-2",
            base_branch=TEST_BASE_BRANCH,
            mergeable=True,
            check_state="success",
            approved=False,
        )
        gh.add_pr(pr)
        # Seed both `review_round` and `retry_count` so both optional
        # context fields ride along on the reviewer's spawn/exit events.
        gh.seed_state(
            2,
            pr_number=_REVIEW_PR_NUMBER,
            review_round=1,
            retry_count=2,
        )
        # Patch _latest_pr_comment_ids so it doesn't touch real GitHub.
        with patch.object(
            workflow, "_latest_pr_comment_ids", return_value=(None, None)
        ):
            self._run(
                lambda: workflow._handle_validating(gh, _TEST_SPEC, issue),
                run_agent=_agent(
                    session_id="sess-review",
                    last_message=REVIEW_APPROVED_MESSAGE,
                ),
                head_shas=[pr.head.sha, pr.head.sha],
            )
        spawns = _events(gh, EVENT_AGENT_SPAWN)
        exits = _events(gh, EVENT_AGENT_EXIT)
        reviewer_spawns = [
            event for event in spawns
            if event[_AGENT_ROLE_KEY] == ROLE_REVIEWER
        ]
        reviewer_exits = [
            event for event in exits
            if event[_AGENT_ROLE_KEY] == ROLE_REVIEWER
        ]
        self.assertEqual(len(reviewer_spawns), 1)
        self.assertEqual(len(reviewer_exits), 1)
        self.assertEqual(reviewer_spawns[0][_STAGE_KEY], LABEL_VALIDATING)
        self.assertEqual(reviewer_spawns[0]["agent"], config.REVIEW_AGENT)
        self.assertEqual(reviewer_spawns[0]["review_round"], 1)
        self.assertEqual(reviewer_spawns[0][_RETRY_COUNT_KEY], 2)
        self.assertEqual(reviewer_exits[0]["review_round"], 1)
        self.assertEqual(reviewer_exits[0][_RETRY_COUNT_KEY], 2)
        self.assertEqual(reviewer_exits[0][_SESSION_ID_KEY], "sess-review")

    def test_dev_resume_spawn_carries_session_id(self) -> None:
        # A resume hands the spawn event the existing session id; the exit
        # event records the (same) live id from the AgentResult.
        gh = FakeGitHubClient()
        issue = make_issue(3, label=LABEL_IMPLEMENTING)
        issue.comments.append(
            FakeComment(id=_RESUME_COMMENT_ID, body="please retry"),
        )
        gh.add_issue(issue)
        gh.seed_state(
            3,
            awaiting_human=True,
            last_action_comment_id=_LAST_ACTION_COMMENT_ID,
            dev_agent="codex", dev_session_id="sess-resume",
        )
        self._run(
            lambda: workflow._handle_implementing(gh, _TEST_SPEC, issue),
            run_agent=_agent(session_id="sess-resume", last_message="q?"),
            has_new_commits=False,
        )
        spawns = _events(gh, EVENT_AGENT_SPAWN)
        self.assertEqual(len(spawns), 1)
        self.assertEqual(spawns[0][_AGENT_ROLE_KEY], ROLE_DEVELOPER)
        self.assertEqual(spawns[0][_SESSION_ID_KEY], "sess-resume")

    def test_timeout_records_timed_out_flag_on_exit(self) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(4, label=LABEL_IMPLEMENTING)
        gh.add_issue(issue)
        self._run(
            lambda: workflow._handle_implementing(gh, _TEST_SPEC, issue),
            run_agent=_agent(timed_out=True, last_message=""),
            has_new_commits=False,
            # before_sha == after_sha: the timeout produced no new commit, so
            # the issue parks (the disposition reads HEAD twice now).
            head_shas=("sha-pre", "sha-pre"),
        )
        exits = _events(gh, EVENT_AGENT_EXIT)
        self.assertEqual(len(exits), 1)
        self.assertTrue(exits[0]["timed_out"])
        self.assertEqual(exits[0]["exit_code"], -1)
