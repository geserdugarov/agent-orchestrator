# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Stage analytics records emitted by the dispatcher and label flips:
`_process_issue` writes one `stage_evaluation` record per handler call
(happy-path, no-stage pickup, error path, backlog-skip short-circuit,
disabled-sink no-op); `set_workflow_label` writes one `stage_enter`
analytics record per non-None label transition."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("ORCHESTRATOR_SKIP_DOTENV", "1")

from orchestrator import analytics, workflow
from orchestrator.github import BACKLOG_LABEL

from tests.fakes import FakeGitHubClient, FakeLabel, make_issue
from tests.workflow_helpers import _TEST_SPEC


class StageEvaluationAnalyticsTest(unittest.TestCase):
    """`_process_issue` times every dispatch and appends a single
    `stage_evaluation` analytics record carrying repo / issue / stage /
    duration_s / result. The record fires on both happy-path and
    exception paths; an unhandled handler exception still propagates so
    the per-issue tick try/except in `workflow.tick` keeps the legacy
    isolation behavior. Backlog-skips are NOT timed -- no handler runs.
    """

    @staticmethod
    def _records(path: Path) -> list[dict]:
        if not path.exists():
            return []
        return [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def test_handler_success_appends_stage_evaluation_record(self) -> None:
        # End-to-end: a labeled issue runs through the dispatcher with
        # the matching handler mocked, and the wrapper writes one
        # `stage_evaluation` line carrying the current label + ok result.
        with tempfile.TemporaryDirectory(prefix="analytics-stageval-") as td:
            path = Path(td) / "analytics.jsonl"
            gh = FakeGitHubClient()
            issue = make_issue(8001, label="implementing")
            gh.add_issue(issue)
            with patch.object(analytics, "ANALYTICS_LOG_PATH", path), \
                 patch.object(workflow, "_handle_implementing"):
                workflow._process_issue(gh, _TEST_SPEC, issue)
            records = [
                r for r in self._records(path)
                if r.get("event") == "stage_evaluation"
                and r.get("issue") == 8001
            ]
        self.assertEqual(len(records), 1)
        rec = records[0]
        self.assertEqual(rec["repo"], "geserdugarov/agent-orchestrator")
        self.assertEqual(rec["stage"], "implementing")
        self.assertEqual(rec["result"], "ok")
        self.assertIn("duration_s", rec)
        self.assertGreaterEqual(rec["duration_s"], 0)

    def test_unlabeled_issue_records_stage_evaluation_with_no_stage(
        self,
    ) -> None:
        # The dispatcher routes a label=None issue to `_handle_pickup`;
        # the `stage_evaluation` record drops the optional `stage` field
        # (build_record's documented contract for None values) so the
        # absence of a workflow label is encoded as "no stage" rather
        # than a string sentinel that downstream aggregations would
        # have to special-case.
        with tempfile.TemporaryDirectory(prefix="analytics-pickup-") as td:
            path = Path(td) / "analytics.jsonl"
            gh = FakeGitHubClient()
            issue = make_issue(8002)
            gh.add_issue(issue)
            with patch.object(analytics, "ANALYTICS_LOG_PATH", path), \
                 patch.object(workflow, "_handle_pickup"):
                workflow._process_issue(gh, _TEST_SPEC, issue)
            records = [
                r for r in self._records(path)
                if r.get("event") == "stage_evaluation"
                and r.get("issue") == 8002
            ]
        self.assertEqual(len(records), 1)
        rec = records[0]
        self.assertNotIn("stage", rec)
        self.assertEqual(rec["result"], "ok")

    def test_handler_exception_records_error_result_and_propagates(
        self,
    ) -> None:
        # The handler raising must NOT suppress the exception: the
        # tick loop's per-issue isolation depends on the dispatcher
        # surfacing failures so they can be logged and the loop
        # continues with the next issue. The record must still land
        # with result=error and the duration captured up to the raise.
        sentinel = RuntimeError("handler blew up")
        with tempfile.TemporaryDirectory(prefix="analytics-err-") as td:
            path = Path(td) / "analytics.jsonl"
            gh = FakeGitHubClient()
            issue = make_issue(8003, label="validating")
            gh.add_issue(issue)
            with patch.object(analytics, "ANALYTICS_LOG_PATH", path), \
                 patch.object(
                     workflow, "_handle_validating", side_effect=sentinel,
                 ):
                with self.assertRaises(RuntimeError) as ctx:
                    workflow._process_issue(gh, _TEST_SPEC, issue)
                self.assertIs(ctx.exception, sentinel)
            records = [
                r for r in self._records(path)
                if r.get("event") == "stage_evaluation"
                and r.get("issue") == 8003
            ]
        self.assertEqual(len(records), 1)
        rec = records[0]
        self.assertEqual(rec["stage"], "validating")
        self.assertEqual(rec["result"], "error")
        self.assertIn("duration_s", rec)

    def test_backlog_skip_does_not_record_stage_evaluation(self) -> None:
        # Backlog parks the issue OUTSIDE the state machine before any
        # handler runs; there is nothing to time. The early return must
        # short-circuit before the timing wrapper writes a record so
        # operators do not see a noisy run of zero-duration evaluations
        # for issues that the orchestrator deliberately ignores.
        with tempfile.TemporaryDirectory(prefix="analytics-backlog-") as td:
            path = Path(td) / "analytics.jsonl"
            gh = FakeGitHubClient()
            issue = make_issue(8004, label="implementing")
            issue.labels.append(FakeLabel(BACKLOG_LABEL))
            gh.add_issue(issue)
            with patch.object(analytics, "ANALYTICS_LOG_PATH", path), \
                 patch.object(workflow, "_handle_implementing") as handler:
                workflow._process_issue(gh, _TEST_SPEC, issue)
            handler.assert_not_called()
        self.assertEqual(self._records(path), [])

    def test_disabled_sink_does_not_write_evaluation_record(self) -> None:
        # The off knob is documented as a silent no-op for the analytics
        # sink. `_process_issue` must respect it so an operator who set
        # ANALYTICS_LOG_PATH=off does not see a phantom file appear.
        with tempfile.TemporaryDirectory(prefix="analytics-off-") as td:
            sentinel = Path(td) / "must-not-be-created.jsonl"
            gh = FakeGitHubClient()
            issue = make_issue(8005, label="implementing")
            gh.add_issue(issue)
            with patch.object(analytics, "ANALYTICS_LOG_PATH", None), \
                 patch.object(workflow, "_handle_implementing"):
                workflow._process_issue(gh, _TEST_SPEC, issue)
            self.assertFalse(sentinel.exists())
            self.assertEqual(list(Path(td).iterdir()), [])


class StageEnterAnalyticsRecordTest(unittest.TestCase):
    """`set_workflow_label` is the single chokepoint for stage transitions;
    every flip emits both the audit `stage_enter` event (to
    `EVENT_LOG_PATH`) and an analytics-compatible `stage_enter` record
    (to `ANALYTICS_LOG_PATH`). Workflow correctness still keys on pinned
    GitHub state; the analytics record is observability only.
    """

    @staticmethod
    def _records(path: Path) -> list[dict]:
        if not path.exists():
            return []
        return [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def test_label_transition_writes_analytics_stage_enter(self) -> None:
        with tempfile.TemporaryDirectory(prefix="analytics-stage-enter-") as td:
            path = Path(td) / "analytics.jsonl"
            with patch.object(analytics, "ANALYTICS_LOG_PATH", path):
                gh = FakeGitHubClient()
                issue = make_issue(8101)
                gh.add_issue(issue)
                gh.set_workflow_label(issue, "implementing")
                gh.set_workflow_label(issue, "validating")
            records = self._records(path)
        self.assertEqual(len(records), 2)
        self.assertEqual(
            [r["stage"] for r in records],
            ["implementing", "validating"],
        )
        for r in records:
            self.assertEqual(r["event"], "stage_enter")
            self.assertEqual(r["issue"], 8101)
            self.assertEqual(r["repo"], "geserdugarov/agent-orchestrator")
            datetime.fromisoformat(r["ts"])

    def test_label_cleared_to_none_does_not_emit_record(self) -> None:
        # Mirrors the existing `_emit_stage_enter` no-op for None labels:
        # clearing a label is not a stage and must not produce a phantom
        # `stage_enter` analytics record.
        with tempfile.TemporaryDirectory(prefix="analytics-stage-none-") as td:
            path = Path(td) / "analytics.jsonl"
            with patch.object(analytics, "ANALYTICS_LOG_PATH", path):
                gh = FakeGitHubClient()
                issue = make_issue(8102, label="implementing")
                gh.add_issue(issue)
                gh.set_workflow_label(issue, None)
        self.assertEqual(self._records(path), [])


if __name__ == "__main__":
    unittest.main()
