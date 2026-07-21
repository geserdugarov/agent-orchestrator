# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Audit-event emission for review verdicts, park-awaiting-human reasons,
PR lifecycle (`pr_opened` / `pr_merged` / `pr_closed_without_merge` /
`merge_attempt`), and the disabled-sink behavioral guarantee."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from orchestrator import config, workflow

from tests.fakes import FakeGitHubClient, FakePR, FakePRRef, make_issue
from tests.workflow_helpers import (
    BACKEND_CLAUDE,
    EVENT_AGENT_EXIT,
    EVENT_AGENT_SPAWN,
    EVENT_PR_CLOSED_WITHOUT_MERGE,
    EVENT_PR_MERGED,
    LABEL_DONE,
    LABEL_IMPLEMENTING,
    LABEL_IN_REVIEW,
    LABEL_RESOLVING_CONFLICT,
    LABEL_VALIDATING,
    REVIEW_APPROVED_MESSAGE,
    STATE_CLOSED,
    TEST_BASE_BRANCH,
    VERDICT_APPROVED,
    VERDICT_CHANGES_REQUESTED,
    VERDICT_UNKNOWN,
    _PatchedWorkflowMixin,
    _TEST_SPEC,
    _agent,
)

EVENT_MERGE_ATTEMPT = "merge_attempt"
EVENT_PARK_AWAITING_HUMAN = "park_awaiting_human"
EVENT_PR_OPENED = "pr_opened"
EVENT_REVIEW_VERDICT = "review_verdict"
EVENT_CONFLICT_ROUND = "conflict_round"

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
    gh = FakeGitHubClient()
    issue = make_issue(5, label=LABEL_VALIDATING)
    gh.add_issue(issue)
    pr = FakePR(
        number=_VERDICT_PR_NUMBER,
        head_branch="orchestrator/geserdugarov__agent-orchestrator/issue-5",
        base_branch=TEST_BASE_BRANCH,
        mergeable=True,
        check_state=CHECK_SUCCESS,
    )
    gh.add_pr(pr)
    gh.seed_state(5, pr_number=_VERDICT_PR_NUMBER, review_round=0)
    return gh, issue, pr, last_message


def _run_verdict(case, gh, issue, pr, last_message: str) -> None:
    with patch.object(
        workflow,
        LATEST_PR_COMMENT_IDS,
        return_value=(None, None),
    ):
        case._run(
            lambda: workflow._handle_validating(gh, _TEST_SPEC, issue),
            run_agent=_agent(
                session_id="sess-review",
                last_message=last_message,
            ),
            head_shas=[pr.head.sha] * 6,
        )


def _events_of(gh: FakeGitHubClient, event_name: str) -> list[dict]:
    return [
        event for event in gh.recorded_events
        if event[KEY_EVENT] == event_name
    ]


def _open_pr(**kwargs) -> FakePR:
    defaults = dict(
        number=PR_NUMBER,
        head_branch=PR_BRANCH,
        head=FakePRRef(sha="abc12345"),
    )
    defaults.update(kwargs)
    return FakePR(**defaults)


def _seed_in_review(issue_number=50, *, pr=None, extra_state=None):
    gh = FakeGitHubClient()
    issue = make_issue(issue_number, label=LABEL_IN_REVIEW)
    gh.add_issue(issue)
    if pr is not None:
        gh.add_pr(pr)
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
    gh.seed_state(issue_number, **state)
    return gh, issue


class ReviewVerdictEventEmissionTest(unittest.TestCase, _PatchedWorkflowMixin):
    """`_handle_validating` emits a `review_verdict` event after parsing the
    reviewer agent's final message, so an operator tailing the JSONL sink
    sees approve/changes-requested decisions inline with the rest of the
    workflow trace.
    """

    def test_approved_verdict_emits_event(self) -> None:
        gh, issue, pr, last = _seeded_verdict(REVIEW_APPROVED_MESSAGE)
        _run_verdict(self, gh, issue, pr, last)
        verdicts = [event for event in gh.recorded_events if event[KEY_EVENT] == EVENT_REVIEW_VERDICT]
        self.assertEqual(len(verdicts), 1)
        verdict = verdicts[0]
        self.assertEqual(verdict[KEY_VERDICT], VERDICT_APPROVED)
        self.assertEqual(verdict[KEY_STAGE], LABEL_VALIDATING)
        self.assertEqual(verdict[KEY_REVIEW_ROUND], 0)
        self.assertEqual(verdict[KEY_PR_NUMBER], _VERDICT_PR_NUMBER)
        self.assertEqual(verdict["session_id"], "sess-review")

    def test_changes_requested_verdict_emits_event(self) -> None:
        gh, issue, pr, last = _seeded_verdict(
            "1. Add a test\n\nVERDICT: CHANGES_REQUESTED",
        )
        _run_verdict(self, gh, issue, pr, last)
        verdicts = [event for event in gh.recorded_events if event[KEY_EVENT] == EVENT_REVIEW_VERDICT]
        self.assertEqual(len(verdicts), 1)
        self.assertEqual(verdicts[0][KEY_VERDICT], VERDICT_CHANGES_REQUESTED)

    def test_unknown_verdict_emits_event(self) -> None:
        gh, issue, pr, last = _seeded_verdict("no marker here")
        _run_verdict(self, gh, issue, pr, last)
        verdicts = [event for event in gh.recorded_events if event[KEY_EVENT] == EVENT_REVIEW_VERDICT]
        self.assertEqual(len(verdicts), 1)
        self.assertEqual(verdicts[0][KEY_VERDICT], VERDICT_UNKNOWN)


class ParkAwaitingHumanEventEmissionTest(unittest.TestCase, _PatchedWorkflowMixin):
    """Every park path (the shared `_park_awaiting_human` helper plus the
    inline `_on_question` / `_on_dirty_worktree` helpers) emits a
    `park_awaiting_human` event tagged with the current stage and an
    optional `reason` so the JSONL sink mirrors the durable `park_reason`
    field for the operator.
    """

    def test_question_park_has_reason_and_stage(self) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(6, label=LABEL_IMPLEMENTING)
        gh.add_issue(issue)
        self._run(
            lambda: workflow._handle_implementing(gh, _TEST_SPEC, issue),
            run_agent=_agent(last_message="please clarify the scope"),
            has_new_commits=False,
        )
        parks = _events_of(gh, EVENT_PARK_AWAITING_HUMAN)
        self.assertEqual(len(parks), 1)
        self.assertEqual(parks[0][KEY_STAGE], LABEL_IMPLEMENTING)
        self.assertEqual(parks[0][KEY_REASON], "agent_question")

    def test_agent_silent_park_carries_reason(self) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(7, label=LABEL_IMPLEMENTING)
        gh.add_issue(issue)
        self._run(
            lambda: workflow._handle_implementing(gh, _TEST_SPEC, issue),
            run_agent=_agent(last_message="", exit_code=1),
            has_new_commits=False,
        )
        parks = _events_of(gh, EVENT_PARK_AWAITING_HUMAN)
        self.assertEqual(len(parks), 1)
        self.assertEqual(parks[0][KEY_REASON], "agent_silent")

    def test_reviewer_timeout_park_carries_reason(self) -> None:
        # Reviewer agent timeout during validating routes through
        # `_park_awaiting_human(reason="reviewer_timeout")` directly.
        gh = FakeGitHubClient()
        issue = make_issue(8, label=LABEL_VALIDATING)
        gh.add_issue(issue)
        gh.seed_state(8, pr_number=_TIMEOUT_PR_NUMBER, review_round=1)
        pr = FakePR(
            number=_TIMEOUT_PR_NUMBER,
            head_branch="orchestrator/geserdugarov__agent-orchestrator/issue-8",
            base_branch=TEST_BASE_BRANCH, mergeable=True, check_state=CHECK_SUCCESS,
        )
        gh.add_pr(pr)
        with patch.object(
            workflow, LATEST_PR_COMMENT_IDS, return_value=(None, None)
        ):
            self._run(
                lambda: workflow._handle_validating(gh, _TEST_SPEC, issue),
                run_agent=_agent(timed_out=True, last_message=""),
                head_shas=[pr.head.sha],
            )
        parks = _events_of(gh, EVENT_PARK_AWAITING_HUMAN)
        self.assertEqual(len(parks), 1)
        self.assertEqual(parks[0][KEY_STAGE], LABEL_VALIDATING)
        self.assertEqual(parks[0][KEY_REASON], "reviewer_timeout")

    def test_review_cap_park_has_reason(self) -> None:
        # `_handle_validating`'s review-cap exhaustion calls
        # `_park_awaiting_human(reason="review_cap")` directly -- a pure
        # shared-helper park path (no transient `state.set("park_reason",
        # ...)` follow-up like the timeout sites have). The emitted event
        # must still carry the reason.
        gh = FakeGitHubClient()
        issue = make_issue(10, label=LABEL_VALIDATING)
        gh.add_issue(issue)
        # Seed review_round at the cap so the very first tick parks.
        gh.seed_state(
            10,
            pr_number=_REVIEW_CAP_PR_NUMBER,
            review_round=config.MAX_REVIEW_ROUNDS,
        )
        pr = FakePR(
            number=_REVIEW_CAP_PR_NUMBER,
            head_branch="orchestrator/geserdugarov__agent-orchestrator/issue-10",
            base_branch=TEST_BASE_BRANCH, mergeable=True, check_state=CHECK_SUCCESS,
        )
        gh.add_pr(pr)
        self._run(
            lambda: workflow._handle_validating(gh, _TEST_SPEC, issue),
            run_agent=_agent(last_message="should not run"),
        )
        parks = _events_of(gh, EVENT_PARK_AWAITING_HUMAN)
        self.assertEqual(len(parks), 1)
        self.assertEqual(parks[0][KEY_STAGE], LABEL_VALIDATING)
        self.assertEqual(parks[0][KEY_REASON], "review_cap")

    def test_push_failed_in_on_commits_carries_reason(self) -> None:
        # `_on_commits` is reached via `_handle_implementing` after the
        # agent committed; a failing push routes through
        # `_park_awaiting_human(reason="push_failed")`. Representative
        # test for a helper-only park outside the validating handler.
        gh = FakeGitHubClient()
        issue = make_issue(_PUSH_FAILED_ISSUE_NUMBER, label=LABEL_IMPLEMENTING)
        gh.add_issue(issue)
        self._run(
            lambda: workflow._handle_implementing(gh, _TEST_SPEC, issue),
            run_agent=_agent(session_id="sess-x", last_message="done"),
            has_new_commits=True,
            push_branch=False,  # simulate push failure
        )
        parks = _events_of(gh, EVENT_PARK_AWAITING_HUMAN)
        self.assertEqual(len(parks), 1)
        self.assertEqual(parks[0][KEY_STAGE], LABEL_IMPLEMENTING)
        self.assertEqual(parks[0][KEY_REASON], "push_failed")

    def test_no_park_event_when_run_does_not_park(self) -> None:
        # A clean approval run flips to in_review without parking; no
        # `park_awaiting_human` event should be recorded.
        gh = FakeGitHubClient()
        issue = make_issue(9, label=LABEL_VALIDATING)
        gh.add_issue(issue)
        pr = FakePR(
            number=_APPROVAL_PR_NUMBER,
            head_branch="orchestrator/geserdugarov__agent-orchestrator/issue-9",
            base_branch=TEST_BASE_BRANCH, mergeable=True, check_state=CHECK_SUCCESS,
        )
        gh.add_pr(pr)
        gh.seed_state(9, pr_number=_APPROVAL_PR_NUMBER, review_round=0)
        with patch.object(
            workflow, LATEST_PR_COMMENT_IDS, return_value=(None, None)
        ):
            self._run(
                lambda: workflow._handle_validating(gh, _TEST_SPEC, issue),
                run_agent=_agent(
                    session_id="sess-r", last_message="ok\n\nVERDICT: APPROVED",
                ),
                head_shas=[pr.head.sha, pr.head.sha],
            )
        self.assertEqual(_events_of(gh, EVENT_PARK_AWAITING_HUMAN), [])


class PrLifecycleEventEmissionTest(unittest.TestCase, _PatchedWorkflowMixin):
    """`pr_opened`, `merge_attempt`, `conflict_round`, `pr_merged`, and
    `pr_closed_without_merge` are emitted from the in_review and
    resolving_conflict handlers so an operator tailing the JSONL sink sees
    the PR-side of each issue's lifecycle (open / conflict round /
    terminal external merge / terminal reject) without scraping the
    orchestrator log. `merge_attempt` is only emitted by
    `_handle_resolving_conflict` for the base rebase; the in_review
    handler is permanently manual-merge-only and never emits it.
    """

    def test_pr_opened_on_fresh_open(self) -> None:
        # _handle_implementing -> _on_commits opens a new PR and emits
        # `pr_opened` with the pr number and branch.
        gh = FakeGitHubClient()
        issue = make_issue(_PR_ISSUE_NUMBER, label=LABEL_IMPLEMENTING)
        gh.add_issue(issue)
        self._run(
            lambda: workflow._handle_implementing(gh, _TEST_SPEC, issue),
            run_agent=_agent(session_id="sess-1", last_message="implemented"),
            # First call: recovered-worktree check (False) -> agent runs;
            # second call: post-agent _has_new_commits check (True) -> push path.
            has_new_commits=[False, True],
            dirty_files=(),
            push_branch=True,
        )
        opened = _events_of(gh, EVENT_PR_OPENED)
        self.assertEqual(len(opened), 1)
        event = opened[0]
        self.assertEqual(event[KEY_STAGE], LABEL_IMPLEMENTING)
        self.assertEqual(event["issue"], _PR_ISSUE_NUMBER)
        self.assertEqual(event[KEY_PR_NUMBER], gh.opened_prs[0].number)
        self.assertEqual(event["branch"], "orchestrator/geserdugarov__agent-orchestrator/issue-50")
        # `sha` carries the PR head sha from `pr.head.sha` so the audit
        # sink can correlate the open event with later merge / review IDs.
        self.assertEqual(event["sha"], gh.opened_prs[0].head.sha)

    def test_pr_opened_not_emitted_when_reusing_pr(self) -> None:
        # Recovery path: an existing open PR is reused rather than opened
        # again. The PR was already announced on its earlier tick, so no
        # `pr_opened` event should fire here.
        gh = FakeGitHubClient()
        issue = make_issue(_REUSED_PR_ISSUE_NUMBER, label=LABEL_IMPLEMENTING)
        gh.add_issue(issue)
        existing = FakePR(
            number=_REUSED_PR_NUMBER,
            head_branch=(
                "orchestrator/geserdugarov__agent-orchestrator/issue-51"
            ),
        )
        gh.existing_open_pr["orchestrator/geserdugarov__agent-orchestrator/issue-51"] = existing
        self._run(
            lambda: workflow._handle_implementing(gh, _TEST_SPEC, issue),
            run_agent=_agent(session_id="sess-1", last_message="implemented"),
            has_new_commits=[False, True],
            push_branch=True,
        )
        self.assertEqual(_events_of(gh, EVENT_PR_OPENED), [])

    def test_mergeable_review_emits_no_merge_event(self) -> None:
        # The orchestrator is manual-merge-only: a mergeable PR in_review
        # never produces a `merge_attempt` or orchestrator-initiated
        # `pr_merged` event. The HITL ping is observable instead.
        pr = _open_pr(approved=True, mergeable=True, check_state=CHECK_SUCCESS)
        gh, issue = _seed_in_review(pr=pr)

        self._run(
            lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
            run_agent=_agent(),
        )

        self.assertEqual(_events_of(gh, EVENT_MERGE_ATTEMPT), [])
        self.assertEqual(_events_of(gh, EVENT_PR_MERGED), [])
        # And no orchestrator-driven label flip to `done`.
        self.assertNotIn((_PR_ISSUE_NUMBER, LABEL_DONE), gh.label_history)

    def test_external_merge_emits_pr_merged(self) -> None:
        # A human (or another bot) merged the PR while we were in_review.
        # The terminal handler stamps `merged_at` and emits `pr_merged`
        # with `merge_method=external`.
        pr = _open_pr(merged=True, state=STATE_CLOSED)
        gh, issue = _seed_in_review(
            pr=pr, extra_state={KEY_CONFLICT_ROUND: 2},
        )
        self._run(
            lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
            run_agent=_agent(),
        )
        merged = _events_of(gh, EVENT_PR_MERGED)
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["merge_method"], "external")
        self.assertEqual(merged[0][KEY_PR_NUMBER], PR_NUMBER)
        self.assertEqual(merged[0]["sha"], "abc12345")
        # In-review terminals carry the round counters from state so an
        # operator tailing the sink can attribute merges to the round count
        # that produced them, not just the issue number.
        self.assertEqual(merged[0][KEY_REVIEW_ROUND], 1)
        self.assertEqual(merged[0][KEY_CONFLICT_ROUND], 2)
        # The orchestrator is permanently manual-merge-only and never
        # emits `merge_attempt` from in_review.
        self.assertEqual(_events_of(gh, EVENT_MERGE_ATTEMPT), [])

    def test_pr_closed_without_merge_on_terminal(self) -> None:
        pr = _open_pr(merged=False, state=STATE_CLOSED)
        gh, issue = _seed_in_review(pr=pr)
        self._run(
            lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
            run_agent=_agent(),
        )
        closed = _events_of(gh, EVENT_PR_CLOSED_WITHOUT_MERGE)
        self.assertEqual(len(closed), 1)
        self.assertEqual(closed[0][KEY_STAGE], LABEL_IN_REVIEW)
        self.assertEqual(closed[0][KEY_PR_NUMBER], PR_NUMBER)

    def test_unmergeable_review_emits_no_round(self) -> None:
        # The orchestrator no longer routes from in_review to
        # `resolving_conflict` on an unmergeable gate. An unmergeable PR
        # parks awaiting human, so no `conflict_round` event is emitted
        # from this stage.
        pr = _open_pr(approved=True, mergeable=False, check_state=CHECK_SUCCESS)
        gh, issue = _seed_in_review(pr=pr)
        self._run(
            lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
            run_agent=_agent(),
        )
        self.assertEqual(_events_of(gh, EVENT_CONFLICT_ROUND), [])
        self.assertNotIn(
            (_PR_ISSUE_NUMBER, LABEL_RESOLVING_CONFLICT),
            gh.label_history,
        )
        self.assertTrue(
            gh.pinned_data(_PR_ISSUE_NUMBER).get("awaiting_human"),
        )


class EventEmissionDisabledTest(unittest.TestCase, _PatchedWorkflowMixin):
    """When EVENT_LOG_PATH is unset (the default), no JSONL file is opened
    and the orchestrator's observable behavior -- comments posted, labels
    set, pinned state written -- is identical to a deployment without the
    audit sink. The in-memory `recorded_events` capture is always populated
    so workflow tests can assert on it without configuring a sink.
    """

    def test_disabled_sink_does_not_change_behavior(self) -> None:
        with tempfile.TemporaryDirectory(prefix="evlog-disabled-") as td:
            sentinel = Path(td) / "should-not-exist.jsonl"
            with patch.object(config, "EVENT_LOG_PATH", None):
                gh = FakeGitHubClient()
                issue = make_issue(
                    _DISABLED_SINK_ISSUE_NUMBER,
                    label=LABEL_IMPLEMENTING,
                )
                gh.add_issue(issue)
                self._run(
                    lambda: workflow._handle_implementing(gh, _TEST_SPEC, issue),
                    run_agent=_agent(last_message="q?"),
                    has_new_commits=False,
                )
            # Disk file is never created.
            self.assertFalse(sentinel.exists())
            # Behavior unchanged: a comment was posted, awaiting_human set,
            # and the various lifecycle events captured in-memory.
            self.assertEqual(len(gh.posted_comments), 1)
            self.assertTrue(
                gh.pinned_data(_DISABLED_SINK_ISSUE_NUMBER).get(
                    "awaiting_human",
                ),
            )
            event_names = {event[KEY_EVENT] for event in gh.recorded_events}
            self.assertIn(EVENT_AGENT_SPAWN, event_names)
            self.assertIn(EVENT_AGENT_EXIT, event_names)
            self.assertIn(EVENT_PARK_AWAITING_HUMAN, event_names)
