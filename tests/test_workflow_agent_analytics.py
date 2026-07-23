# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Tracked-agent analytics records and audit events."""
from __future__ import annotations

import unittest
from dataclasses import dataclass
from unittest.mock import patch

from orchestrator import config, workflow
from orchestrator.agents import AgentResult

from tests.fakes import FakeGitHubClient, FakeIssue, FakePR, make_issue

from tests import workflow_agent_analytics_test_support as support

EVENT_AGENT_EXIT = support.EVENT_AGENT_EXIT
EVENT_AGENT_SPAWN = support.EVENT_AGENT_SPAWN
LABEL_IMPLEMENTING = support.LABEL_IMPLEMENTING
LABEL_VALIDATING = support.LABEL_VALIDATING
REVIEW_APPROVED_MESSAGE = support.REVIEW_APPROVED_MESSAGE
ROLE_DEVELOPER = support.ROLE_DEVELOPER
ROLE_REVIEWER = support.ROLE_REVIEWER
TEST_BASE_BRANCH = support.TEST_BASE_BRANCH
TEST_REPO_SLUG = support.TEST_REPO_SLUG
_AGENT_ROLE_KEY = support._AGENT_ROLE_KEY
_ANALYTICS_FILENAME = support._ANALYTICS_FILENAME
_AUDIT_ISSUE_NUMBER = support._AUDIT_ISSUE_NUMBER
_CLAUDE_CACHE_WRITE_TOKENS = support._CLAUDE_CACHE_WRITE_TOKENS
_CLAUDE_INPUT_TOKENS = support._CLAUDE_INPUT_TOKENS
_CLAUDE_MODEL = support._CLAUDE_MODEL
_CLAUDE_OUTPUT_TOKENS = support._CLAUDE_OUTPUT_TOKENS
_COST_USD_KEY = support._COST_USD_KEY
_DISABLED_SINK_ISSUE_NUMBER = support._DISABLED_SINK_ISSUE_NUMBER
_EVENT_KEY = support._EVENT_KEY
_IMPLEMENTING_ANALYTICS_ISSUE_NUMBER = support._IMPLEMENTING_ANALYTICS_ISSUE_NUMBER
_INPUT_TOKENS_KEY = support._INPUT_TOKENS_KEY
_OUTPUT_TOKENS_KEY = support._OUTPUT_TOKENS_KEY
_PatchedWorkflowMixin = support._PatchedWorkflowMixin
_REDACTION_ISSUE_NUMBER = support._REDACTION_ISSUE_NUMBER
_REPORTED_COST_USD = support._REPORTED_COST_USD
_REVIEW_ISSUE_NUMBER = support._REVIEW_ISSUE_NUMBER
_REVIEW_PR_NUMBER = support._REVIEW_PR_NUMBER
_STAGE_KEY = support._STAGE_KEY
_TEST_SPEC = support._TEST_SPEC
_TIMEOUT_ISSUE_NUMBER = support._TIMEOUT_ISSUE_NUMBER
_analytics_path = support._analytics_path
_analytics_records = support._analytics_records
_assert_redacted_record = support._assert_redacted_record
_claude_stdout = support._claude_stdout


@dataclass(frozen=True)
class _IssueScenario:
    github: FakeGitHubClient
    issue: FakeIssue


def _seed_issue(
    issue_number: int,
    label: str,
    body: str = "test body",
) -> _IssueScenario:
    github = FakeGitHubClient()
    issue = make_issue(issue_number, label=label, body=body)
    github.add_issue(issue)
    return _IssueScenario(github=github, issue=issue)


def _assert_record_context(
    case: unittest.TestCase,
    record: dict,
) -> None:
    case.assertEqual(record[_EVENT_KEY], EVENT_AGENT_EXIT)
    case.assertEqual(record["repo"], TEST_REPO_SLUG)
    case.assertEqual(
        record["issue"],
        _IMPLEMENTING_ANALYTICS_ISSUE_NUMBER,
    )
    case.assertEqual(record[_STAGE_KEY], LABEL_IMPLEMENTING)
    case.assertEqual(record[_AGENT_ROLE_KEY], ROLE_DEVELOPER)
    case.assertEqual(record["backend"], config.DEV_AGENT)
    case.assertEqual(record["agent_spec"], config.DEV_AGENT_SPEC)


def _assert_record_exit(
    case: unittest.TestCase,
    record: dict,
) -> None:
    case.assertEqual(record["session_id"], "sess-impl")
    case.assertNotIn("resume_session_id", record)
    case.assertEqual(record["review_round"], 0)
    case.assertEqual(record["exit_code"], 0)
    case.assertFalse(record["timed_out"])
    case.assertGreaterEqual(record["duration_s"], 0)
    case.assertEqual(record["retry_count"], 1)


def _assert_record_usage(
    case: unittest.TestCase,
    record: dict,
) -> None:
    case.assertEqual(record[_INPUT_TOKENS_KEY], _CLAUDE_INPUT_TOKENS)
    case.assertEqual(record[_OUTPUT_TOKENS_KEY], _CLAUDE_OUTPUT_TOKENS)
    case.assertEqual(record["cache_read_tokens"], 100)
    case.assertEqual(
        record["cache_write_tokens"],
        _CLAUDE_CACHE_WRITE_TOKENS,
    )
    case.assertEqual(record["models"], [_CLAUDE_MODEL])
    case.assertEqual(record["turns"], 2)
    case.assertEqual(record["cost_source"], "reported")
    case.assertAlmostEqual(record[_COST_USD_KEY], _REPORTED_COST_USD)


def _assert_reviewer_record(
    case: unittest.TestCase,
    record: dict,
) -> None:
    case.assertEqual(record[_STAGE_KEY], LABEL_VALIDATING)
    case.assertEqual(record["backend"], config.REVIEW_AGENT)
    case.assertEqual(record["agent_spec"], config.REVIEW_AGENT_SPEC)
    case.assertEqual(record["review_round"], 2)
    case.assertEqual(record["retry_count"], 3)
    case.assertEqual(record["session_id"], "sess-review")
    case.assertNotIn("resume_session_id", record)


class AgentAnalyticsTest(unittest.TestCase, _PatchedWorkflowMixin):
    """`_run_agent_tracked` appends a single analytics record per agent
    exit, carrying the configured spec, resume/session context, retry
    budget, reviewer round, duration, exit metadata, parsed token
    counts, model list, cost, and cost_source -- and never the prompt,
    raw stdout, stderr, or any auth header. The existing audit
    `agent_spawn` / `agent_exit` events must continue to fire unchanged.
    """

    def test_implementing_spawn_appends_record(self) -> None:
        path = _analytics_path(self, "analytics-impl-")
        scenario = _seed_issue(
            _IMPLEMENTING_ANALYTICS_ISSUE_NUMBER,
            LABEL_IMPLEMENTING,
        )
        self._run(
            lambda: workflow._handle_implementing(
                scenario.github,
                _TEST_SPEC,
                scenario.issue,
            ),
            run_agent=AgentResult(
                session_id="sess-impl",
                last_message="open question?",
                exit_code=0,
                timed_out=False,
                stdout=_claude_stdout(
                    total_cost_usd=_REPORTED_COST_USD,
                ),
                stderr="",
            ),
            has_new_commits=False,
            analytics_log_path=path,
        )

        records = _analytics_records(path)
        self.assertEqual(len(records), 1)
        record = records[0]
        _assert_record_context(self, record)
        _assert_record_exit(self, record)
        _assert_record_usage(self, record)

    def test_excludes_prompt_stdout_stderr_secrets(self) -> None:
        path = _analytics_path(self, "analytics-redaction-")
        raw_stdout = _claude_stdout()
        secret_marker = "ghp_DEADBEEFDEADBEEFDEADBEEFDEADBEEFDEAD"
        scenario = _seed_issue(
            _REDACTION_ISSUE_NUMBER,
            LABEL_IMPLEMENTING,
            body=f"please use token {secret_marker}",
        )
        self._run(
            lambda: workflow._handle_implementing(
                scenario.github,
                _TEST_SPEC,
                scenario.issue,
            ),
            run_agent=AgentResult(
                session_id="sess-redact",
                last_message="q?",
                exit_code=0,
                timed_out=False,
                stdout=raw_stdout,
                stderr=f"WARN missing scope for {secret_marker}",
            ),
            has_new_commits=False,
            analytics_log_path=path,
        )

        record = _analytics_records(path)[0]
        _assert_redacted_record(
            self,
            record,
            raw_stdout,
            secret_marker,
        )

    def test_reviewer_record_carries_round_and_resume(self) -> None:
        path = _analytics_path(self, "analytics-review-")
        scenario = _seed_issue(
            _REVIEW_ISSUE_NUMBER,
            LABEL_VALIDATING,
        )
        pull_request = FakePR(
            number=_REVIEW_PR_NUMBER,
            head_branch=(
                "orchestrator/geserdugarov__agent-orchestrator/issue-103"
            ),
            base_branch=TEST_BASE_BRANCH,
            mergeable=True,
            check_state="success",
            approved=False,
        )
        scenario.github.add_pr(pull_request)
        scenario.github.seed_state(
            _REVIEW_ISSUE_NUMBER,
            pr_number=_REVIEW_PR_NUMBER,
            review_round=2,
            retry_count=3,
        )
        with patch.object(
            workflow,
            "_latest_pr_comment_ids",
            return_value=(None, None),
        ):
            self._run(
                lambda: workflow._handle_validating(
                    scenario.github,
                    _TEST_SPEC,
                    scenario.issue,
                ),
                run_agent=AgentResult(
                    session_id="sess-review",
                    last_message=REVIEW_APPROVED_MESSAGE,
                    exit_code=0,
                    timed_out=False,
                    stdout=_claude_stdout(msg_id="msg-review"),
                    stderr="",
                ),
                head_shas=[
                    pull_request.head.sha,
                    pull_request.head.sha,
                ],
                analytics_log_path=path,
            )

        reviewer_record = next(
            record
            for record in _analytics_records(path)
            if record.get(_AGENT_ROLE_KEY) == ROLE_REVIEWER
        )
        _assert_reviewer_record(self, reviewer_record)

    def test_timeout_records_exit_metadata_no_cost(self) -> None:
        path = _analytics_path(self, "analytics-timeout-")
        scenario = _seed_issue(
            _TIMEOUT_ISSUE_NUMBER,
            LABEL_IMPLEMENTING,
        )
        self._run(
            lambda: workflow._handle_implementing(
                scenario.github,
                _TEST_SPEC,
                scenario.issue,
            ),
            run_agent=AgentResult(
                session_id=None,
                last_message="",
                exit_code=-1,
                timed_out=True,
                stdout="",
                stderr="",
            ),
            has_new_commits=False,
            head_shas=("sha-pre", "sha-pre"),
            analytics_log_path=path,
        )

        records = _analytics_records(path)
        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertEqual(record["exit_code"], -1)
        self.assertTrue(record["timed_out"])
        self.assertEqual(record["cost_source"], "no-usage")
        self.assertNotIn(_COST_USD_KEY, record)
        self.assertEqual(record[_INPUT_TOKENS_KEY], 0)
        self.assertEqual(record[_OUTPUT_TOKENS_KEY], 0)

    def test_audit_events_unchanged_with_record(self) -> None:
        path = _analytics_path(self, "analytics-audit-")
        scenario = _seed_issue(
            _AUDIT_ISSUE_NUMBER,
            LABEL_IMPLEMENTING,
        )
        self._run(
            lambda: workflow._handle_implementing(
                scenario.github,
                _TEST_SPEC,
                scenario.issue,
            ),
            run_agent=AgentResult(
                session_id="sess-x",
                last_message="q?",
                exit_code=0,
                timed_out=False,
                stdout=_claude_stdout(),
                stderr="",
            ),
            has_new_commits=False,
            analytics_log_path=path,
        )

        event_names = [
            event[_EVENT_KEY]
            for event in scenario.github.recorded_events
        ]
        self.assertEqual(event_names.count(EVENT_AGENT_SPAWN), 1)
        self.assertEqual(event_names.count(EVENT_AGENT_EXIT), 1)
        exit_event = next(
            event
            for event in scenario.github.recorded_events
            if event[_EVENT_KEY] == EVENT_AGENT_EXIT
        )
        self.assertEqual(exit_event["session_id"], "sess-x")
        self.assertEqual(exit_event["exit_code"], 0)
        self.assertEqual(len(_analytics_records(path)), 1)

    def test_disabled_sink_writes_no_analytics_file(self) -> None:
        sentinel = _analytics_path(
            self,
            "analytics-off-",
            "must-not-exist.jsonl",
        )
        scenario = _seed_issue(
            _DISABLED_SINK_ISSUE_NUMBER,
            LABEL_IMPLEMENTING,
        )
        self._run(
            lambda: workflow._handle_implementing(
                scenario.github,
                _TEST_SPEC,
                scenario.issue,
            ),
            run_agent=AgentResult(
                session_id="sess-off",
                last_message="q?",
                exit_code=0,
                timed_out=False,
                stdout=_claude_stdout(),
                stderr="",
            ),
            has_new_commits=False,
        )
        self.assertFalse(sentinel.exists())
        self.assertEqual(list(sentinel.parent.iterdir()), [])
        self.assertIn(
            EVENT_AGENT_EXIT,
            {
                event[_EVENT_KEY]
                for event in scenario.github.recorded_events
            },
        )
