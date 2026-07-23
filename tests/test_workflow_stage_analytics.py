# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Stage analytics records emitted by the dispatcher and label flips:
`_process_issue` writes one `stage_evaluation` record per handler call
(happy-path, no-stage pickup, error path, backlog-skip short-circuit,
disabled-sink no-op); `set_workflow_label` writes one `stage_enter`
analytics record per non-None label transition."""
from __future__ import annotations

import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

from orchestrator import analytics, workflow
from orchestrator.github import BACKLOG_LABEL, PAUSED_LABEL

from tests.fakes import FakeGitHubClient, FakeLabel, make_issue
from tests.workflow_helpers import (
    EVENT_STAGE_ENTER,
    EVENT_STAGE_EVALUATION,
    LABEL_IMPLEMENTING,
    LABEL_VALIDATING,
    TEST_REPO_SLUG,
    _TEST_SPEC,
    _analytics_records,
)


_ANALYTICS_FILENAME = "analytics.jsonl"
_ANALYTICS_PATH_ATTR = "ANALYTICS_LOG_PATH"
_STAGE_KEY = "stage"
_HARD_SKIPPED_ISSUE = 8004
_SUCCESS_ISSUE = 8001
_UNLABELED_ISSUE = 8002
_ERROR_ISSUE = 8003
_DISABLED_SINK_ISSUE = 8005
_STAGE_ENTER_ISSUE = 8101
_LABEL_CLEAR_ISSUE = 8102


def _stage_evaluations(path: Path, issue_number: int) -> list[dict]:
    return [
        record for record in _analytics_records(path)
        if record.get("event") == EVENT_STAGE_EVALUATION
        and record.get("issue") == issue_number
    ]


def _process_hard_skipped_issue(skip_label: str) -> tuple[MagicMock, list[dict]]:
    with tempfile.TemporaryDirectory(prefix="analytics-skip-") as temp_dir:
        path = Path(temp_dir) / _ANALYTICS_FILENAME
        gh = FakeGitHubClient()
        issue = make_issue(_HARD_SKIPPED_ISSUE, label=LABEL_IMPLEMENTING)
        issue.labels.append(FakeLabel(skip_label))
        gh.add_issue(issue)
        handler_mock = MagicMock()
        with patch.object(analytics, _ANALYTICS_PATH_ATTR, path), patch.object(
            workflow,
            "_handle_implementing",
            handler_mock,
        ):
            workflow._process_issue(gh, _TEST_SPEC, issue)
        return handler_mock, _analytics_records(path)


def _process_error(gh: FakeGitHubClient, issue) -> RuntimeError:
    try:
        workflow._process_issue(gh, _TEST_SPEC, issue)
    except RuntimeError as error:
        return error
    raise AssertionError("the stage handler did not propagate its error")


def _stage_enter_projection(record: dict) -> tuple:
    datetime.fromisoformat(record["ts"])
    return record["event"], record["issue"], record["repo"]


class StageEvaluationAnalyticsTest(unittest.TestCase):
    """`_process_issue` times every dispatch and appends a single
    `stage_evaluation` analytics record carrying repo / issue / stage /
    duration_s / result. The record fires on both happy-path and
    exception paths; an unhandled handler exception still propagates so
    the per-issue tick try/except in `workflow.tick` keeps the legacy
    isolation behavior. Backlog-skips are NOT timed -- no handler runs.
    """

    def test_success_appends_evaluation_record(self) -> None:
        # End-to-end: a labeled issue runs through the dispatcher with
        # the matching handler mocked, and the wrapper writes one
        # `stage_evaluation` line carrying the current label + ok result.
        with tempfile.TemporaryDirectory(prefix="analytics-stageval-") as td:
            path = Path(td) / _ANALYTICS_FILENAME
            gh = FakeGitHubClient()
            issue = make_issue(_SUCCESS_ISSUE, label=LABEL_IMPLEMENTING)
            gh.add_issue(issue)
            with patch.object(analytics, _ANALYTICS_PATH_ATTR, path), \
                 patch.object(workflow, "_handle_implementing"):
                workflow._process_issue(gh, _TEST_SPEC, issue)
            record = _stage_evaluations(path, _SUCCESS_ISSUE)[0]
        self.assertEqual(record["repo"], TEST_REPO_SLUG)
        self.assertEqual(record[_STAGE_KEY], LABEL_IMPLEMENTING)
        self.assertEqual(record["result"], "ok")
        self.assertIn("duration_s", record)
        self.assertGreaterEqual(record["duration_s"], 0)

    def test_unlabeled_issue_records_no_stage(
        self,
    ) -> None:
        # The dispatcher routes a label=None issue to `_handle_pickup`;
        # the `stage_evaluation` record drops the optional `stage` field
        # (build_record's documented contract for None values) so the
        # absence of a workflow label is encoded as "no stage" rather
        # than a string sentinel that downstream aggregations would
        # have to special-case.
        with tempfile.TemporaryDirectory(prefix="analytics-pickup-") as td:
            path = Path(td) / _ANALYTICS_FILENAME
            gh = FakeGitHubClient()
            issue = make_issue(_UNLABELED_ISSUE)
            gh.add_issue(issue)
            with patch.object(analytics, _ANALYTICS_PATH_ATTR, path), \
                 patch.object(workflow, "_handle_pickup"):
                workflow._process_issue(gh, _TEST_SPEC, issue)
            record = _stage_evaluations(path, _UNLABELED_ISSUE)[0]
        self.assertNotIn(_STAGE_KEY, record)
        self.assertEqual(record["result"], "ok")

    def test_error_is_recorded_and_propagated(
        self,
    ) -> None:
        # The handler raising must NOT suppress the exception: the
        # tick loop's per-issue isolation depends on the dispatcher
        # surfacing failures so they can be logged and the loop
        # continues with the next issue. The record must still land
        # with result=error and the duration captured up to the raise.
        with tempfile.TemporaryDirectory(prefix="analytics-err-") as td:
            path = Path(td) / _ANALYTICS_FILENAME
            gh = FakeGitHubClient()
            issue = make_issue(_ERROR_ISSUE, label=LABEL_VALIDATING)
            gh.add_issue(issue)
            with (
                patch.object(analytics, _ANALYTICS_PATH_ATTR, path),
                patch.object(
                    workflow,
                    "_handle_validating",
                    side_effect=RuntimeError("handler blew up"),
                ),
            ):
                self.assertEqual(
                    str(_process_error(gh, issue)),
                    "handler blew up",
                )
            record = _stage_evaluations(path, _ERROR_ISSUE)[0]
        self.assertEqual(record[_STAGE_KEY], LABEL_VALIDATING)
        self.assertEqual(record["result"], "error")
        self.assertIn("duration_s", record)

    def test_hard_skip_records_no_evaluation(self) -> None:
        # A hard-skip control label (`backlog` / `paused`) parks the issue
        # OUTSIDE the state machine before any handler runs; there is nothing
        # to time. The early return must short-circuit before the timing
        # wrapper writes a record so operators do not see a noisy run of
        # zero-duration evaluations for issues the orchestrator ignores.
        for skip_label in (BACKLOG_LABEL, PAUSED_LABEL):
            with self.subTest(label=skip_label):
                handler_mock, records = _process_hard_skipped_issue(skip_label)
                handler_mock.assert_not_called()
                self.assertEqual(records, [])

    def test_disabled_sink_writes_no_evaluation(self) -> None:
        # The off knob is documented as a silent no-op for the analytics
        # sink. `_process_issue` must respect it so an operator who set
        # ANALYTICS_LOG_PATH=off does not see a phantom file appear.
        with tempfile.TemporaryDirectory(prefix="analytics-off-") as td:
            sentinel = Path(td) / "must-not-be-created.jsonl"
            gh = FakeGitHubClient()
            issue = make_issue(_DISABLED_SINK_ISSUE, label=LABEL_IMPLEMENTING)
            gh.add_issue(issue)
            with patch.object(analytics, _ANALYTICS_PATH_ATTR, None), \
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

    def test_label_transition_writes_stage_enter(self) -> None:
        with tempfile.TemporaryDirectory(prefix="analytics-stage-enter-") as td:
            path = Path(td) / _ANALYTICS_FILENAME
            with patch.object(analytics, _ANALYTICS_PATH_ATTR, path):
                gh = FakeGitHubClient()
                issue = make_issue(_STAGE_ENTER_ISSUE)
                gh.add_issue(issue)
                gh.set_workflow_label(issue, LABEL_IMPLEMENTING)
                gh.set_workflow_label(issue, LABEL_VALIDATING)
            records = _analytics_records(path)
        self.assertEqual(len(records), 2)
        self.assertEqual(
            [record[_STAGE_KEY] for record in records],
            [LABEL_IMPLEMENTING, LABEL_VALIDATING],
        )
        self.assertEqual(
            list(map(_stage_enter_projection, records)),
            [
                (EVENT_STAGE_ENTER, _STAGE_ENTER_ISSUE, TEST_REPO_SLUG),
                (EVENT_STAGE_ENTER, _STAGE_ENTER_ISSUE, TEST_REPO_SLUG),
            ],
        )

    def test_label_clear_emits_no_record(self) -> None:
        # Mirrors the existing `_emit_stage_enter` no-op for None labels:
        # clearing a label is not a stage and must not produce a phantom
        # `stage_enter` analytics record.
        with tempfile.TemporaryDirectory(prefix="analytics-stage-none-") as td:
            path = Path(td) / _ANALYTICS_FILENAME
            with patch.object(analytics, _ANALYTICS_PATH_ATTR, path):
                gh = FakeGitHubClient()
                issue = make_issue(_LABEL_CLEAR_ISSUE, label=LABEL_IMPLEMENTING)
                gh.add_issue(issue)
                gh.set_workflow_label(issue, None)
        self.assertEqual(_analytics_records(path), [])


if __name__ == "__main__":
    unittest.main()
