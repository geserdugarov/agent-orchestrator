# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Models and values for terminal-drain workflow tests."""
from __future__ import annotations

from dataclasses import dataclass

from orchestrator import workflow
from orchestrator.github import PinnedState

from tests import fakes as _fakes
from tests import workflow_event_values as _events
from tests import workflow_other_labels as _other_labels
from tests import workflow_patch_models as _patch_models
from tests import workflow_patch_runner as _patch_runner
from tests import workflow_repo_values as _repo
from tests import workflow_stage_labels as _stage_labels
from tests import workflow_value_helpers as _value_helpers


EVENT_PR_CLOSED_WITHOUT_MERGE = _events.EVENT_PR_CLOSED_WITHOUT_MERGE
EVENT_PR_MERGED = _events.EVENT_PR_MERGED
LABEL_DONE = _other_labels.LABEL_DONE
LABEL_REJECTED = _other_labels.LABEL_REJECTED
LABEL_RESOLVING_CONFLICT = _other_labels.LABEL_RESOLVING_CONFLICT
LABEL_FIXING = _stage_labels.LABEL_FIXING
LABEL_IN_REVIEW = _stage_labels.LABEL_IN_REVIEW
STATE_CLOSED = _repo.STATE_CLOSED
STATE_OPEN = _repo.STATE_OPEN
_TEST_SPEC = _repo._TEST_SPEC
_PatchedWorkflowMixin = _patch_runner._PatchedWorkflowMixin
_agent = _patch_models._agent
_issue_branch = _value_helpers._issue_branch
_state_with_pr_number = _value_helpers._state_with_pr_number
FakeGitHubClient = _fakes.FakeGitHubClient
FakePR = _fakes.FakePR
FakePRRef = _fakes.FakePRRef
make_issue = _fakes.make_issue

DEFAULT_HEAD_SHA = "cafe1234"
MERGE_METHOD_EXTERNAL = "external"
_CLEANUP_MOCK_KEY = "_cleanup_terminal_branch"
_EVENT_KEY = "event"
_STAGE_KEY = "stage"
_CONFLICT_ROUND_KEY = "conflict_round"
_NO_PR_ISSUE_NUMBER = 310
_NO_PR_NUMBER = 31000
_OPEN_PR_ISSUE_NUMBER = 311
_OPEN_PR_NUMBER = 31100
_MERGED_PR_ISSUE_NUMBER = 312
_MERGED_PR_NUMBER = 31200
_CLOSED_PR_ISSUE_NUMBER = 313
_CLOSED_PR_NUMBER = 31300
_MANUALLY_CLOSED_ISSUE_NUMBER = 314
_MANUALLY_CLOSED_PR_NUMBER = 31400
_ALREADY_CLOSED_ISSUE_NUMBER = 315
_ALREADY_CLOSED_PR_NUMBER = 31500
_CONFLICT_MERGED_ISSUE_NUMBER = 316
_CONFLICT_MERGED_PR_NUMBER = 31600
_CONFLICT_CLOSED_ISSUE_NUMBER = 317
_CONFLICT_CLOSED_PR_NUMBER = 31700
_REVIEW_MERGED_ISSUE_NUMBER = 318
_REVIEW_MERGED_PR_NUMBER = 31800
_RECEIPT_MERGED_ISSUE_NUMBER = 320
_RECEIPT_MERGED_PR_NUMBER = 32000
_RECEIPT_CLOSED_ISSUE_NUMBER = 321
_RECEIPT_CLOSED_PR_NUMBER = 32100
_RECEIPT_MANUAL_ISSUE_NUMBER = 322
_RECEIPT_MANUAL_PR_NUMBER = 32200


@dataclass(frozen=True)
class _DrainContext:
    gh: FakeGitHubClient
    issue: object
    state: PinnedState
    pr: FakePR
    stage: str


@dataclass(frozen=True)
class _DrainScenario:
    issue_number: int
    pr_number: int
    merged: bool
    pr_state: str
    stage: str
    issue_closed: bool = False
    sha: str = DEFAULT_HEAD_SHA


@dataclass(frozen=True)
class _DrainResult:
    context: _DrainContext
    mocks: dict


class _DrainTerminalCall:
    def __init__(self, context: _DrainContext) -> None:
        self._context = context
        self.was_drained = False

    def __call__(self) -> None:
        self.was_drained = workflow._drain_review_pr_terminals(
            self._context.gh,
            _TEST_SPEC,
            self._context.issue,
            self._context.state,
            self._context.pr,
            stage=self._context.stage,
        )


def _seed_terminal(
    scenario: _DrainScenario,
    **state_values,
) -> _DrainContext:
    github = FakeGitHubClient()
    issue = make_issue(scenario.issue_number, label=scenario.stage)
    issue.closed = scenario.issue_closed
    github.add_issue(issue)
    pull_request = FakePR(
        number=scenario.pr_number,
        head_branch=_issue_branch(scenario.issue_number),
        head=FakePRRef(sha=scenario.sha),
        merged=scenario.merged,
        state=scenario.pr_state,
    )
    github.add_pr(pull_request)
    state = _state_with_pr_number(
        github,
        scenario.issue_number,
        scenario.pr_number,
        **state_values,
    )
    return _DrainContext(github, issue, state, pull_request, scenario.stage)


class _DrainTestMixin(_PatchedWorkflowMixin):
    def _drain(self, context: _DrainContext) -> _DrainResult:
        terminal_call = _DrainTerminalCall(context)
        mocks = self._run(terminal_call, run_agent=_agent())
        self.assertTrue(terminal_call.was_drained)
        return _DrainResult(context, mocks)

    def _only_event(self, drain_result: _DrainResult, event_name: str) -> dict:
        events = [
            event
            for event in drain_result.context.gh.recorded_events
            if event[_EVENT_KEY] == event_name
        ]
        self.assertEqual(len(events), 1)
        return events[0]

    def _assert_cleanup(
        self,
        drain_result: _DrainResult,
        *,
        label: str,
        state_key: str,
    ) -> None:
        context = drain_result.context
        self.assertIn((context.issue.number, label), context.gh.label_history)
        self.assertIn(state_key, context.state.data)
        self.assertTrue(context.issue.closed)
        drain_result.mocks[_CLEANUP_MOCK_KEY].assert_called_once_with(
            context.gh,
            _TEST_SPEC,
            context.issue.number,
            branch=_issue_branch(context.issue.number),
        )

    def _terminal_event(
        self,
        scenario: _DrainScenario,
        event_name: str,
        **state_values,
    ) -> dict:
        context = _seed_terminal(scenario, **state_values)
        drain_result = self._drain(context)
        return self._only_event(drain_result, event_name)

    def _assert_usage_receipt(self, scenario: _DrainScenario) -> None:
        context = _seed_terminal(
            scenario,
            conflict_round=0,
            issue_agent_runs=2,
            issue_total_tokens=1000,
            issue_total_cost_usd=0.5,
            issue_cost_sources=["reported"],
        )
        self._drain(context)
        receipts = [
            body
            for issue_number, body in context.gh.posted_comments
            if issue_number == scenario.issue_number and body.startswith(":receipt:")
        ]
        self.assertEqual(len(receipts), 1)
        self.assertIn(
            "this issue: 2 agent runs · 1,000 tokens · $0.50",
            receipts[0],
        )
        receipt_comment = next(
            comment
            for comment in context.issue.comments
            if comment.body.startswith(":receipt:")
        )
        self.assertIn(
            receipt_comment.id,
            context.gh.pinned_data(scenario.issue_number).get(
                "orchestrator_comment_ids",
                [],
            ),
        )


RECEIPT_SCENARIOS = (
    _DrainScenario(
        _RECEIPT_MERGED_ISSUE_NUMBER,
        _RECEIPT_MERGED_PR_NUMBER,
        True,
        STATE_CLOSED,
        LABEL_IN_REVIEW,
    ),
    _DrainScenario(
        _RECEIPT_CLOSED_ISSUE_NUMBER,
        _RECEIPT_CLOSED_PR_NUMBER,
        False,
        STATE_CLOSED,
        LABEL_FIXING,
    ),
    _DrainScenario(
        _RECEIPT_MANUAL_ISSUE_NUMBER,
        _RECEIPT_MANUAL_PR_NUMBER,
        False,
        STATE_OPEN,
        LABEL_RESOLVING_CONFLICT,
        issue_closed=True,
    ),
)
