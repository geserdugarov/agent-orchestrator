# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Shared values and fakes for workflow event-emission tests."""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from orchestrator import config

from tests import fakes as _fakes
from tests import workflow_helpers as _helpers


EVENT_AGENT_EXIT = _helpers.EVENT_AGENT_EXIT
EVENT_AGENT_SPAWN = _helpers.EVENT_AGENT_SPAWN
EVENT_STAGE_ENTER = _helpers.EVENT_STAGE_ENTER
LABEL_DECOMPOSING = _helpers.LABEL_DECOMPOSING
LABEL_DOCUMENTING = _helpers.LABEL_DOCUMENTING
LABEL_IMPLEMENTING = _helpers.LABEL_IMPLEMENTING
LABEL_VALIDATING = _helpers.LABEL_VALIDATING
REVIEW_APPROVED_MESSAGE = _helpers.REVIEW_APPROVED_MESSAGE
ROLE_DEVELOPER = _helpers.ROLE_DEVELOPER
ROLE_REVIEWER = _helpers.ROLE_REVIEWER
TEST_BASE_BRANCH = _helpers.TEST_BASE_BRANCH
TEST_REPO_SLUG = _helpers.TEST_REPO_SLUG
_PatchedWorkflowMixin = _helpers._PatchedWorkflowMixin
_TEST_SPEC = _helpers._TEST_SPEC
_agent = _helpers._agent

FakeComment = _fakes.FakeComment
FakeGitHubClient = _fakes.FakeGitHubClient
FakePR = _fakes.FakePR
make_issue = _fakes.make_issue

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
        event
        for event in gh.recorded_events
        if event[_EVENT_KEY] == event_name
    ]


def _write_stage_events(path: Path) -> list[str]:
    with patch.object(config, "EVENT_LOG_PATH", path):
        github = FakeGitHubClient()
        issue = make_issue(7)
        github.add_issue(issue)
        github.set_workflow_label(issue, LABEL_IMPLEMENTING)
        github.set_workflow_label(issue, LABEL_VALIDATING)
        github.set_workflow_label(issue, LABEL_DOCUMENTING)
    return path.read_text(encoding="utf-8").splitlines()


def _stage_record_projection(record: dict) -> tuple:
    timestamp = datetime.fromisoformat(record["ts"])
    return (
        record[_STAGE_KEY],
        record[_EVENT_KEY],
        record["issue"],
        record["repo"],
        timestamp.tzinfo,
    )


def _parse_records(lines: list[str]) -> list[dict]:
    return [json.loads(line) for line in lines]


def _only_role_event(
    gh: FakeGitHubClient,
    event_name: str,
    role: str,
) -> dict:
    matching_events = [
        event
        for event in _events(gh, event_name)
        if event[_AGENT_ROLE_KEY] == role
    ]
    if len(matching_events) != 1:
        raise AssertionError(f"expected one {event_name} event for {role}")
    return matching_events[0]
