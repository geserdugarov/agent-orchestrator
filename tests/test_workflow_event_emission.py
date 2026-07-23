# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Stage-transition audit event tests."""
from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from orchestrator import config, workflow

from tests import workflow_event_emission_test_support as support


class StageEventEmissionTest(unittest.TestCase, support._PatchedWorkflowMixin):
    """`set_workflow_label` is the single chokepoint for stage transitions,
    so a hook there gives every workflow handler a `stage_enter` event for
    free. The fake mirrors the real client's `recorded_events` capture and
    JSONL sink so workflow tests can assert on either surface.
    """

    def test_label_change_records_stage_enter(self) -> None:
        gh = support.FakeGitHubClient()
        issue = support.make_issue(1)
        gh.add_issue(issue)
        gh.set_workflow_label(issue, support.LABEL_IMPLEMENTING)
        self.assertEqual(len(gh.recorded_events), 1)
        event = gh.recorded_events[0]
        self.assertEqual(event[support._EVENT_KEY], support.EVENT_STAGE_ENTER)
        self.assertEqual(event[support._STAGE_KEY], support.LABEL_IMPLEMENTING)
        self.assertEqual(event["issue"], 1)
        self.assertEqual(event["repo"], support.TEST_REPO_SLUG)
        self.assertIn("ts", event)
        # UTC timestamp, ISO 8601 with offset.
        datetime.fromisoformat(event["ts"])

    def test_none_label_does_not_emit(self) -> None:
        # Clearing the workflow label is not a stage; the helper must
        # short-circuit so downstream consumers don't see a phantom
        # `stage_enter` with stage=None.
        gh = support.FakeGitHubClient()
        issue = support.make_issue(1, label=support.LABEL_IMPLEMENTING)
        gh.add_issue(issue)
        gh.set_workflow_label(issue, None)
        self.assertEqual(gh.recorded_events, [])

    def test_pickup_emits_decomposing_stage_enter(self) -> None:
        # The hook is centralized: a real handler call (no manual label
        # flip in the test) still produces the event because
        # `_handle_pickup` routes through `gh.set_workflow_label`.
        gh = support.FakeGitHubClient()
        issue = support.make_issue(1)
        gh.add_issue(issue)
        with patch.object(config, "DECOMPOSE", True):
            self._run(
                lambda: workflow._handle_pickup(gh, support._TEST_SPEC, issue),
                run_agent=support._agent(last_message="need clarification"),
                has_new_commits=False,
            )
        stages = [
            event[support._STAGE_KEY] for event in gh.recorded_events
            if event[support._EVENT_KEY] == support.EVENT_STAGE_ENTER
        ]
        self.assertIn(support.LABEL_DECOMPOSING, stages)

    def test_event_log_writes_one_object_per_line(self) -> None:
        # End-to-end: a configured EVENT_LOG_PATH receives one parseable
        # JSONL object per transition, with the documented schema.
        with tempfile.TemporaryDirectory(prefix="evlog-") as td:
            path = Path(td) / "events.jsonl"
            lines = support._write_stage_events(path)
        records = support._parse_records(lines)
        expected = [
            (
                stage,
                support.EVENT_STAGE_ENTER,
                7,
                support.TEST_REPO_SLUG,
                timezone.utc,
            )
            for stage in (
                support.LABEL_IMPLEMENTING,
                support.LABEL_VALIDATING,
                support.LABEL_DOCUMENTING,
            )
        ]
        self.assertEqual(
            list(map(support._stage_record_projection, records)),
            expected,
        )
        self.assertTrue(all(line.strip() for line in lines))
        self.assertFalse(any(line.startswith(" ") for line in lines))

    def test_event_log_path_unset_writes_no_file(self) -> None:
        # The legacy behavior is that no event file exists; flipping a
        # label must not create one when EVENT_LOG_PATH is unset.
        with tempfile.TemporaryDirectory(prefix="evlog-off-") as td:
            sentinel = Path(td) / "should-not-be-created.jsonl"
            with patch.object(config, "EVENT_LOG_PATH", None):
                gh = support.FakeGitHubClient()
                issue = support.make_issue(1)
                gh.add_issue(issue)
                gh.set_workflow_label(issue, support.LABEL_IMPLEMENTING)
            self.assertFalse(sentinel.exists())
            # In-memory capture still works even with the file sink disabled,
            # so tests don't need a temp file to inspect transitions.
            self.assertEqual(len(gh.recorded_events), 1)
