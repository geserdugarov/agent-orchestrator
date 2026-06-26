# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Audit-event emission from `set_workflow_label` and `_run_agent_tracked`:
one `stage_enter` per label flip, paired `agent_spawn`/`agent_exit` per
agent run, optional `session_id`/`review_round`/`retry_count` context, and
the JSONL sink driven by `EVENT_LOG_PATH`."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("ORCHESTRATOR_SKIP_DOTENV", "1")

from orchestrator import config, workflow

from tests.fakes import (
    FakeComment,
    FakeGitHubClient,
    FakePR,
    make_issue,
)
from tests.workflow_helpers import _PatchedWorkflowMixin, _TEST_SPEC, _agent


class StageEventEmissionTest(unittest.TestCase, _PatchedWorkflowMixin):
    """`set_workflow_label` is the single chokepoint for stage transitions,
    so a hook there gives every workflow handler a `stage_enter` event for
    free. The fake mirrors the real client's `recorded_events` capture and
    JSONL sink so workflow tests can assert on either surface.
    """

    def test_set_workflow_label_records_stage_enter_event(self) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(1)
        gh.add_issue(issue)
        gh.set_workflow_label(issue, "implementing")
        self.assertEqual(len(gh.recorded_events), 1)
        ev = gh.recorded_events[0]
        self.assertEqual(ev["event"], "stage_enter")
        self.assertEqual(ev["stage"], "implementing")
        self.assertEqual(ev["issue"], 1)
        self.assertEqual(ev["repo"], "geserdugarov/agent-orchestrator")
        self.assertIn("ts", ev)
        # UTC timestamp, ISO 8601 with offset.
        datetime.fromisoformat(ev["ts"])

    def test_none_label_does_not_emit(self) -> None:
        # Clearing the workflow label is not a stage; the helper must
        # short-circuit so downstream consumers don't see a phantom
        # `stage_enter` with stage=None.
        gh = FakeGitHubClient()
        issue = make_issue(1, label="implementing")
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
        stages = [e["stage"] for e in gh.recorded_events if e["event"] == "stage_enter"]
        self.assertIn("decomposing", stages)

    def test_event_log_path_writes_one_jsonl_object_per_line(self) -> None:
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
                gh.set_workflow_label(issue, "implementing")
                gh.set_workflow_label(issue, "validating")
                gh.set_workflow_label(issue, "documenting")
            # File closed on context exit -- read it back, parse line by line.
            lines = path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 3)
            records = [json.loads(line) for line in lines]
            self.assertEqual(
                [r["stage"] for r in records],
                ["implementing", "validating", "documenting"],
            )
            for r in records:
                self.assertEqual(r["event"], "stage_enter")
                self.assertEqual(r["issue"], 7)
                self.assertEqual(r["repo"], "geserdugarov/agent-orchestrator")
                # ts must be a valid ISO-8601 UTC timestamp.
                ts = datetime.fromisoformat(r["ts"])
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
                gh.set_workflow_label(issue, "implementing")
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

    @staticmethod
    def _events(gh, event_name: str) -> list[dict]:
        return [e for e in gh.recorded_events if e["event"] == event_name]

    def test_fresh_developer_spawn_emits_paired_lifecycle_events(self) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(1, label="implementing")
        gh.add_issue(issue)
        self._run(
            lambda: workflow._handle_implementing(gh, _TEST_SPEC, issue),
            run_agent=_agent(session_id="sess-dev", last_message="q?"),
            has_new_commits=False,
        )
        spawns = self._events(gh, "agent_spawn")
        exits = self._events(gh, "agent_exit")
        self.assertEqual(len(spawns), 1)
        self.assertEqual(len(exits), 1)
        spawn = spawns[0]
        ex = exits[0]
        self.assertEqual(spawn["stage"], "implementing")
        self.assertEqual(spawn["agent_role"], "developer")
        self.assertEqual(spawn["agent"], config.DEV_AGENT)
        self.assertNotIn("session_id", spawn)  # fresh spawn -- no resume id
        self.assertEqual(ex["session_id"], "sess-dev")
        self.assertEqual(ex["exit_code"], 0)
        self.assertFalse(ex["timed_out"])
        self.assertIn("duration_s", ex)
        self.assertGreaterEqual(ex["duration_s"], 0)
        # retry_count is incremented to 1 by `_check_and_increment_retry_budget`
        # BEFORE the spawn, so the recorded value is what the agent ran under.
        self.assertEqual(spawn["retry_count"], 1)
        self.assertEqual(ex["retry_count"], 1)

    def test_reviewer_spawn_carries_review_round_and_retry_count(self) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(2, label="validating")
        gh.add_issue(issue)
        pr = FakePR(
            number=42,
            head_branch="orchestrator/geserdugarov__agent-orchestrator/issue-2",
            base_branch="main",
            mergeable=True,
            check_state="success",
            approved=False,
        )
        gh.add_pr(pr)
        # Seed both `review_round` and `retry_count` so both optional
        # context fields ride along on the reviewer's spawn/exit events.
        gh.seed_state(2, pr_number=42, review_round=1, retry_count=2)
        # Patch _latest_pr_comment_ids so it doesn't touch real GitHub.
        with patch.object(
            workflow, "_latest_pr_comment_ids", return_value=(None, None)
        ):
            self._run(
                lambda: workflow._handle_validating(gh, _TEST_SPEC, issue),
                run_agent=_agent(
                    session_id="sess-review", last_message="VERDICT: APPROVED",
                ),
                head_shas=[pr.head.sha, pr.head.sha],
            )
        spawns = self._events(gh, "agent_spawn")
        exits = self._events(gh, "agent_exit")
        reviewer_spawns = [s for s in spawns if s["agent_role"] == "reviewer"]
        reviewer_exits = [e for e in exits if e["agent_role"] == "reviewer"]
        self.assertEqual(len(reviewer_spawns), 1)
        self.assertEqual(len(reviewer_exits), 1)
        self.assertEqual(reviewer_spawns[0]["stage"], "validating")
        self.assertEqual(reviewer_spawns[0]["agent"], config.REVIEW_AGENT)
        self.assertEqual(reviewer_spawns[0]["review_round"], 1)
        self.assertEqual(reviewer_spawns[0]["retry_count"], 2)
        self.assertEqual(reviewer_exits[0]["review_round"], 1)
        self.assertEqual(reviewer_exits[0]["retry_count"], 2)
        self.assertEqual(reviewer_exits[0]["session_id"], "sess-review")

    def test_dev_resume_spawn_carries_session_id(self) -> None:
        # A resume hands the spawn event the existing session id; the exit
        # event records the (same) live id from the AgentResult.
        gh = FakeGitHubClient()
        issue = make_issue(3, label="implementing")
        issue.comments.append(FakeComment(id=2000, body="please retry"))
        gh.add_issue(issue)
        gh.seed_state(
            3, awaiting_human=True, last_action_comment_id=1500,
            dev_agent="codex", dev_session_id="sess-resume",
        )
        self._run(
            lambda: workflow._handle_implementing(gh, _TEST_SPEC, issue),
            run_agent=_agent(session_id="sess-resume", last_message="q?"),
            has_new_commits=False,
        )
        spawns = self._events(gh, "agent_spawn")
        self.assertEqual(len(spawns), 1)
        self.assertEqual(spawns[0]["agent_role"], "developer")
        self.assertEqual(spawns[0]["session_id"], "sess-resume")

    def test_timeout_records_timed_out_flag_on_exit(self) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(4, label="implementing")
        gh.add_issue(issue)
        self._run(
            lambda: workflow._handle_implementing(gh, _TEST_SPEC, issue),
            run_agent=_agent(timed_out=True, last_message=""),
            has_new_commits=False,
            # before_sha == after_sha: the timeout produced no new commit, so
            # the issue parks (the disposition reads HEAD twice now).
            head_shas=("sha-pre", "sha-pre"),
        )
        exits = self._events(gh, "agent_exit")
        self.assertEqual(len(exits), 1)
        self.assertTrue(exits[0]["timed_out"])
        self.assertEqual(exits[0]["exit_code"], -1)
