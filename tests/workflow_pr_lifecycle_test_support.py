# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Shared fixtures for workflow PR-lifecycle event tests."""
from __future__ import annotations

from unittest.mock import patch

from orchestrator import workflow

from tests import fakes as _fakes
from tests import workflow_helpers as _helpers


BACKEND_CLAUDE = _helpers.BACKEND_CLAUDE
EVENT_AGENT_EXIT = _helpers.EVENT_AGENT_EXIT
EVENT_AGENT_SPAWN = _helpers.EVENT_AGENT_SPAWN
EVENT_PR_CLOSED_WITHOUT_MERGE = _helpers.EVENT_PR_CLOSED_WITHOUT_MERGE
EVENT_PR_MERGED = _helpers.EVENT_PR_MERGED
LABEL_DONE = _helpers.LABEL_DONE
LABEL_IMPLEMENTING = _helpers.LABEL_IMPLEMENTING
LABEL_IN_REVIEW = _helpers.LABEL_IN_REVIEW
LABEL_RESOLVING_CONFLICT = _helpers.LABEL_RESOLVING_CONFLICT
LABEL_VALIDATING = _helpers.LABEL_VALIDATING
REVIEW_APPROVED_MESSAGE = _helpers.REVIEW_APPROVED_MESSAGE
STATE_CLOSED = _helpers.STATE_CLOSED
TEST_BASE_BRANCH = _helpers.TEST_BASE_BRANCH
VERDICT_APPROVED = _helpers.VERDICT_APPROVED
VERDICT_CHANGES_REQUESTED = _helpers.VERDICT_CHANGES_REQUESTED
VERDICT_UNKNOWN = _helpers.VERDICT_UNKNOWN
_PatchedWorkflowMixin = _helpers._PatchedWorkflowMixin
_TEST_SPEC = _helpers._TEST_SPEC
_agent = _helpers._agent

FakeGitHubClient = _fakes.FakeGitHubClient
FakePR = _fakes.FakePR
FakePRRef = _fakes.FakePRRef
make_issue = _fakes.make_issue

EVENT_MERGE_ATTEMPT = "merge_attempt"
EVENT_PARK_AWAITING_HUMAN = "park_awaiting_human"
EVENT_PR_OPENED = "pr_opened"
EVENT_REVIEW_VERDICT = "review_verdict"
EVENT_CONFLICT_ROUND = "conflict_round"
VALIDATION_HEAD_PROBE_COUNT = 6

KEY_CONFLICT_ROUND = EVENT_CONFLICT_ROUND
KEY_EVENT = "event"
KEY_PR_NUMBER = "pr_number"
KEY_REASON = "reason"
KEY_REVIEW_ROUND = "review_round"
KEY_STAGE = "stage"
KEY_VERDICT = "verdict"

CHECK_SUCCESS = "success"
LATEST_PR_COMMENT_IDS = "_latest_pr_comment_ids"
PR_BRANCH = "orchestrator/geserdugarov__agent-orchestrator/issue-50"
PR_NUMBER = 500
_VERDICT_PR_NUMBER = 99
_TIMEOUT_PR_NUMBER = 42
_REVIEW_CAP_PR_NUMBER = 33
_APPROVAL_PR_NUMBER = 11
_PUSH_FAILED_ISSUE_NUMBER = 11
_PR_ISSUE_NUMBER = 50
_REUSED_PR_ISSUE_NUMBER = 51
_REUSED_PR_NUMBER = 123
_DISABLED_SINK_ISSUE_NUMBER = 20


def _seeded_verdict(last_message: str):
    github = FakeGitHubClient()
    issue = make_issue(5, label=LABEL_VALIDATING)
    github.add_issue(issue)
    pull_request = FakePR(
        number=_VERDICT_PR_NUMBER,
        head_branch="orchestrator/geserdugarov__agent-orchestrator/issue-5",
        base_branch=TEST_BASE_BRANCH,
        mergeable=True,
        check_state=CHECK_SUCCESS,
    )
    github.add_pr(pull_request)
    github.seed_state(5, pr_number=_VERDICT_PR_NUMBER, review_round=0)
    return github, issue, pull_request, last_message


def _run_verdict(case, github, issue, pull_request, last_message: str) -> None:
    with patch.object(
        workflow,
        LATEST_PR_COMMENT_IDS,
        return_value=(None, None),
    ):
        case._run(
            lambda: workflow._handle_validating(github, _TEST_SPEC, issue),
            run_agent=_agent(
                session_id="sess-review",
                last_message=last_message,
            ),
            head_shas=[
                pull_request.head.sha
                for _probe_index in range(VALIDATION_HEAD_PROBE_COUNT)
            ],
        )


def _events_of(gh: FakeGitHubClient, event_name: str) -> list[dict]:
    return [
        event
        for event in gh.recorded_events
        if event[KEY_EVENT] == event_name
    ]


def _only_event(gh: FakeGitHubClient, event_name: str) -> dict:
    events = _events_of(gh, event_name)
    if len(events) != 1:
        raise AssertionError(f"expected one {event_name} event")
    return events[0]


def _open_pr(**kwargs) -> FakePR:
    defaults = dict(
        number=PR_NUMBER,
        head_branch=PR_BRANCH,
        head=FakePRRef(sha="abc12345"),
    )
    defaults.update(kwargs)
    return FakePR(**defaults)


def _seed_in_review(issue_number=50, *, pr=None, extra_state=None):
    github = FakeGitHubClient()
    issue = make_issue(issue_number, label=LABEL_IN_REVIEW)
    github.add_issue(issue)
    if pr is not None:
        github.add_pr(pr)
    state = dict(
        branch=PR_BRANCH,
        dev_agent=BACKEND_CLAUDE,
        dev_session_id="dev-sess",
        review_round=1,
    )
    if pr is not None:
        state[KEY_PR_NUMBER] = pr.number
    if extra_state:
        state.update(extra_state)
    github.seed_state(issue_number, **state)
    return github, issue
