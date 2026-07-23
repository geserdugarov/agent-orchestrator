# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Pull-request lifecycle and disabled event-sink tests."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from orchestrator import config, workflow

from tests import workflow_pr_lifecycle_test_support as support


class PrLifecycleEventEmissionTest(unittest.TestCase, support._PatchedWorkflowMixin):
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
        gh = support.FakeGitHubClient()
        issue = support.make_issue(support._PR_ISSUE_NUMBER, label=support.LABEL_IMPLEMENTING)
        gh.add_issue(issue)
        self._run(
            lambda: workflow._handle_implementing(gh, support._TEST_SPEC, issue),
            run_agent=support._agent(session_id="sess-1", last_message="implemented"),
            # First call: recovered-worktree check (False) -> agent runs;
            # second call: post-agent _has_new_commits check (True) -> push path.
            has_new_commits=[False, True],
            dirty_files=(),
            push_branch=True,
        )
        opened = support._events_of(gh, support.EVENT_PR_OPENED)
        self.assertEqual(len(opened), 1)
        event = opened[0]
        self.assertEqual(event[support.KEY_STAGE], support.LABEL_IMPLEMENTING)
        self.assertEqual(event["issue"], support._PR_ISSUE_NUMBER)
        self.assertEqual(event[support.KEY_PR_NUMBER], gh.opened_prs[0].number)
        self.assertEqual(event["branch"], "orchestrator/geserdugarov__agent-orchestrator/issue-50")
        # `sha` carries the PR head sha from `pr.head.sha` so the audit
        # sink can correlate the open event with later merge / review IDs.
        self.assertEqual(event["sha"], gh.opened_prs[0].head.sha)

    def test_pr_opened_not_emitted_when_reusing_pr(self) -> None:
        # Recovery path: an existing open PR is reused rather than opened
        # again. The PR was already announced on its earlier tick, so no
        # `pr_opened` event should fire here.
        gh = support.FakeGitHubClient()
        issue = support.make_issue(support._REUSED_PR_ISSUE_NUMBER, label=support.LABEL_IMPLEMENTING)
        gh.add_issue(issue)
        existing = support.FakePR(
            number=support._REUSED_PR_NUMBER,
            head_branch=(
                "orchestrator/geserdugarov__agent-orchestrator/issue-51"
            ),
        )
        gh.existing_open_pr["orchestrator/geserdugarov__agent-orchestrator/issue-51"] = existing
        self._run(
            lambda: workflow._handle_implementing(gh, support._TEST_SPEC, issue),
            run_agent=support._agent(session_id="sess-1", last_message="implemented"),
            has_new_commits=[False, True],
            push_branch=True,
        )
        self.assertEqual(support._events_of(gh, support.EVENT_PR_OPENED), [])

    def test_mergeable_review_emits_no_merge_event(self) -> None:
        # The orchestrator is manual-merge-only: a mergeable PR in_review
        # never produces a `merge_attempt` or orchestrator-initiated
        # `pr_merged` event. The HITL ping is observable instead.
        pr = support._open_pr(approved=True, mergeable=True, check_state=support.CHECK_SUCCESS)
        gh, issue = support._seed_in_review(pr=pr)

        self._run(
            lambda: workflow._handle_in_review(gh, support._TEST_SPEC, issue),
            run_agent=support._agent(),
        )

        self.assertEqual(support._events_of(gh, support.EVENT_MERGE_ATTEMPT), [])
        self.assertEqual(support._events_of(gh, support.EVENT_PR_MERGED), [])
        # And no orchestrator-driven label flip to `done`.
        self.assertNotIn((support._PR_ISSUE_NUMBER, support.LABEL_DONE), gh.label_history)

    def test_external_merge_emits_pr_merged(self) -> None:
        # A human (or another bot) merged the PR while we were in_review.
        # The terminal handler stamps `merged_at` and emits `pr_merged`
        # with `merge_method=external`.
        pr = support._open_pr(merged=True, state=support.STATE_CLOSED)
        gh, issue = support._seed_in_review(
            pr=pr, extra_state={support.KEY_CONFLICT_ROUND: 2},
        )
        self._run(
            lambda: workflow._handle_in_review(gh, support._TEST_SPEC, issue),
            run_agent=support._agent(),
        )
        merged_event = support._only_event(gh, support.EVENT_PR_MERGED)
        self.assertEqual(merged_event["merge_method"], "external")
        self.assertEqual(merged_event[support.KEY_PR_NUMBER], support.PR_NUMBER)
        self.assertEqual(merged_event["sha"], "abc12345")
        # In-review terminals carry the round counters from state so an
        # operator tailing the sink can attribute merges to the round count
        # that produced them, not just the issue number.
        self.assertEqual(merged_event[support.KEY_REVIEW_ROUND], 1)
        self.assertEqual(merged_event[support.KEY_CONFLICT_ROUND], 2)
        # The orchestrator is permanently manual-merge-only and never
        # emits `merge_attempt` from in_review.
        self.assertEqual(support._events_of(gh, support.EVENT_MERGE_ATTEMPT), [])

    def test_pr_closed_without_merge_on_terminal(self) -> None:
        pr = support._open_pr(merged=False, state=support.STATE_CLOSED)
        gh, issue = support._seed_in_review(pr=pr)
        self._run(
            lambda: workflow._handle_in_review(gh, support._TEST_SPEC, issue),
            run_agent=support._agent(),
        )
        closed = support._events_of(gh, support.EVENT_PR_CLOSED_WITHOUT_MERGE)
        self.assertEqual(len(closed), 1)
        self.assertEqual(closed[0][support.KEY_STAGE], support.LABEL_IN_REVIEW)
        self.assertEqual(closed[0][support.KEY_PR_NUMBER], support.PR_NUMBER)

    def test_unmergeable_review_emits_no_round(self) -> None:
        # The orchestrator no longer routes from in_review to
        # `resolving_conflict` on an unmergeable gate. An unmergeable PR
        # parks awaiting human, so no `conflict_round` event is emitted
        # from this stage.
        pr = support._open_pr(approved=True, mergeable=False, check_state=support.CHECK_SUCCESS)
        gh, issue = support._seed_in_review(pr=pr)
        self._run(
            lambda: workflow._handle_in_review(gh, support._TEST_SPEC, issue),
            run_agent=support._agent(),
        )
        self.assertEqual(support._events_of(gh, support.EVENT_CONFLICT_ROUND), [])
        self.assertNotIn(
            (support._PR_ISSUE_NUMBER, support.LABEL_RESOLVING_CONFLICT),
            gh.label_history,
        )
        self.assertTrue(
            gh.pinned_data(support._PR_ISSUE_NUMBER).get("awaiting_human"),
        )


class EventEmissionDisabledTest(unittest.TestCase, support._PatchedWorkflowMixin):
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
                gh = support.FakeGitHubClient()
                issue = support.make_issue(
                    support._DISABLED_SINK_ISSUE_NUMBER,
                    label=support.LABEL_IMPLEMENTING,
                )
                gh.add_issue(issue)
                self._run(
                    lambda: workflow._handle_implementing(gh, support._TEST_SPEC, issue),
                    run_agent=support._agent(last_message="q?"),
                    has_new_commits=False,
                )
            # Disk file is never created.
            self.assertFalse(sentinel.exists())
            # Behavior unchanged: a comment was posted, awaiting_human set,
            # and the various lifecycle events captured in-memory.
            self.assertEqual(len(gh.posted_comments), 1)
            self.assertTrue(
                gh.pinned_data(support._DISABLED_SINK_ISSUE_NUMBER).get(
                    "awaiting_human",
                ),
            )
            event_names = {event[support.KEY_EVENT] for event in gh.recorded_events}
            self.assertIn(support.EVENT_AGENT_SPAWN, event_names)
            self.assertIn(support.EVENT_AGENT_EXIT, event_names)
            self.assertIn(support.EVENT_PARK_AWAITING_HUMAN, event_names)
