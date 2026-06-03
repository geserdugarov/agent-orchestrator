# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Audit-event emission for review verdicts, park-awaiting-human reasons,
PR lifecycle (`pr_opened` / `pr_merged` / `pr_closed_without_merge` /
`merge_attempt`), and the disabled-sink behavioral guarantee."""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("ORCHESTRATOR_SKIP_DOTENV", "1")

from orchestrator import config, workflow

from tests.fakes import FakeGitHubClient, FakePR, FakePRRef, make_issue
from tests.workflow_helpers import _PatchedWorkflowMixin, _TEST_SPEC, _agent


class ReviewVerdictEventEmissionTest(unittest.TestCase, _PatchedWorkflowMixin):
    """`_handle_validating` emits a `review_verdict` event after parsing the
    reviewer agent's final message, so an operator tailing the JSONL sink
    sees approve/changes-requested decisions inline with the rest of the
    workflow trace.
    """

    def _seeded(self, last_message: str):
        gh = FakeGitHubClient()
        issue = make_issue(5, label="validating")
        gh.add_issue(issue)
        pr = FakePR(
            number=99,
            head_branch="orchestrator/issue-5",
            base_branch="main",
            mergeable=True,
            check_state="success",
        )
        gh.add_pr(pr)
        gh.seed_state(5, pr_number=99, review_round=0)
        return gh, issue, pr, last_message

    def _run_validating(self, gh, issue, pr, last_message: str):
        # Enough head_shas to cover both the approved branch (reviewed_sha +
        # squash inputs) and the changes_requested branch (before/after the
        # dev fix). Identical SHAs across the sequence mean the dev fix is
        # treated as a no-op question (we only care about the verdict event).
        with patch.object(
            workflow, "_latest_pr_comment_ids", return_value=(None, None)
        ):
            self._run(
                lambda: workflow._handle_validating(gh, _TEST_SPEC, issue),
                run_agent=_agent(
                    session_id="sess-review", last_message=last_message,
                ),
                head_shas=[pr.head.sha] * 6,
            )

    def test_approved_verdict_emits_event(self) -> None:
        gh, issue, pr, last = self._seeded("LGTM\n\nVERDICT: APPROVED")
        self._run_validating(gh, issue, pr, last)
        verdicts = [e for e in gh.recorded_events if e["event"] == "review_verdict"]
        self.assertEqual(len(verdicts), 1)
        v = verdicts[0]
        self.assertEqual(v["verdict"], "approved")
        self.assertEqual(v["stage"], "validating")
        self.assertEqual(v["review_round"], 0)
        self.assertEqual(v["pr_number"], 99)
        self.assertEqual(v["session_id"], "sess-review")

    def test_changes_requested_verdict_emits_event(self) -> None:
        gh, issue, pr, last = self._seeded(
            "1. Add a test\n\nVERDICT: CHANGES_REQUESTED",
        )
        self._run_validating(gh, issue, pr, last)
        verdicts = [e for e in gh.recorded_events if e["event"] == "review_verdict"]
        self.assertEqual(len(verdicts), 1)
        self.assertEqual(verdicts[0]["verdict"], "changes_requested")

    def test_unknown_verdict_emits_event(self) -> None:
        gh, issue, pr, last = self._seeded("no marker here")
        self._run_validating(gh, issue, pr, last)
        verdicts = [e for e in gh.recorded_events if e["event"] == "review_verdict"]
        self.assertEqual(len(verdicts), 1)
        self.assertEqual(verdicts[0]["verdict"], "unknown")


class ParkAwaitingHumanEventEmissionTest(unittest.TestCase, _PatchedWorkflowMixin):
    """Every park path (the shared `_park_awaiting_human` helper plus the
    inline `_on_question` / `_on_dirty_worktree` helpers) emits a
    `park_awaiting_human` event tagged with the current stage and an
    optional `reason` so the JSONL sink mirrors the durable `park_reason`
    field for the operator.
    """

    @staticmethod
    def _parks(gh) -> list[dict]:
        return [e for e in gh.recorded_events if e["event"] == "park_awaiting_human"]

    def test_agent_question_emits_park_event_with_reason_and_stage(self) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(6, label="implementing")
        gh.add_issue(issue)
        self._run(
            lambda: workflow._handle_implementing(gh, _TEST_SPEC, issue),
            run_agent=_agent(last_message="please clarify the scope"),
            has_new_commits=False,
        )
        parks = self._parks(gh)
        self.assertEqual(len(parks), 1)
        self.assertEqual(parks[0]["stage"], "implementing")
        self.assertEqual(parks[0]["reason"], "agent_question")

    def test_agent_silent_emits_park_event_with_reason(self) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(7, label="implementing")
        gh.add_issue(issue)
        self._run(
            lambda: workflow._handle_implementing(gh, _TEST_SPEC, issue),
            run_agent=_agent(last_message="", exit_code=1),
            has_new_commits=False,
        )
        parks = self._parks(gh)
        self.assertEqual(len(parks), 1)
        self.assertEqual(parks[0]["reason"], "agent_silent")

    def test_reviewer_timeout_emits_park_event_with_reason(self) -> None:
        # Reviewer agent timeout during validating routes through
        # `_park_awaiting_human(reason="reviewer_timeout")` directly.
        gh = FakeGitHubClient()
        issue = make_issue(8, label="validating")
        gh.add_issue(issue)
        gh.seed_state(8, pr_number=42, review_round=1)
        pr = FakePR(
            number=42, head_branch="orchestrator/issue-8",
            base_branch="main", mergeable=True, check_state="success",
        )
        gh.add_pr(pr)
        with patch.object(
            workflow, "_latest_pr_comment_ids", return_value=(None, None)
        ):
            self._run(
                lambda: workflow._handle_validating(gh, _TEST_SPEC, issue),
                run_agent=_agent(timed_out=True, last_message=""),
                head_shas=[pr.head.sha],
            )
        parks = self._parks(gh)
        self.assertEqual(len(parks), 1)
        self.assertEqual(parks[0]["stage"], "validating")
        self.assertEqual(parks[0]["reason"], "reviewer_timeout")

    def test_shared_helper_park_carries_reason_for_review_cap(self) -> None:
        # `_handle_validating`'s review-cap exhaustion calls
        # `_park_awaiting_human(reason="review_cap")` directly -- a pure
        # shared-helper park path (no transient `state.set("park_reason",
        # ...)` follow-up like the timeout sites have). The emitted event
        # must still carry the reason.
        gh = FakeGitHubClient()
        issue = make_issue(10, label="validating")
        gh.add_issue(issue)
        # Seed review_round at the cap so the very first tick parks.
        gh.seed_state(
            10, pr_number=33, review_round=config.MAX_REVIEW_ROUNDS,
        )
        pr = FakePR(
            number=33, head_branch="orchestrator/issue-10",
            base_branch="main", mergeable=True, check_state="success",
        )
        gh.add_pr(pr)
        self._run(
            lambda: workflow._handle_validating(gh, _TEST_SPEC, issue),
            run_agent=_agent(last_message="should not run"),
        )
        parks = self._parks(gh)
        self.assertEqual(len(parks), 1)
        self.assertEqual(parks[0]["stage"], "validating")
        self.assertEqual(parks[0]["reason"], "review_cap")

    def test_push_failed_in_on_commits_carries_reason(self) -> None:
        # `_on_commits` is reached via `_handle_implementing` after the
        # agent committed; a failing push routes through
        # `_park_awaiting_human(reason="push_failed")`. Representative
        # test for a helper-only park outside the validating handler.
        gh = FakeGitHubClient()
        issue = make_issue(11, label="implementing")
        gh.add_issue(issue)
        self._run(
            lambda: workflow._handle_implementing(gh, _TEST_SPEC, issue),
            run_agent=_agent(session_id="sess-x", last_message="done"),
            has_new_commits=True,
            push_branch=False,  # simulate push failure
        )
        parks = self._parks(gh)
        self.assertEqual(len(parks), 1)
        self.assertEqual(parks[0]["stage"], "implementing")
        self.assertEqual(parks[0]["reason"], "push_failed")

    def test_no_park_event_when_run_does_not_park(self) -> None:
        # A clean approval run flips to in_review without parking; no
        # `park_awaiting_human` event should be recorded.
        gh = FakeGitHubClient()
        issue = make_issue(9, label="validating")
        gh.add_issue(issue)
        pr = FakePR(
            number=11, head_branch="orchestrator/issue-9",
            base_branch="main", mergeable=True, check_state="success",
        )
        gh.add_pr(pr)
        gh.seed_state(9, pr_number=11, review_round=0)
        with patch.object(
            workflow, "_latest_pr_comment_ids", return_value=(None, None)
        ):
            self._run(
                lambda: workflow._handle_validating(gh, _TEST_SPEC, issue),
                run_agent=_agent(
                    session_id="sess-r", last_message="ok\n\nVERDICT: APPROVED",
                ),
                head_shas=[pr.head.sha, pr.head.sha],
            )
        self.assertEqual(self._parks(gh), [])


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

    BRANCH = "orchestrator/issue-50"
    PR_NUMBER = 500

    @staticmethod
    def _events_of(gh, event_name: str) -> list[dict]:
        return [e for e in gh.recorded_events if e["event"] == event_name]

    def _open_pr(self, **kwargs):
        defaults = dict(
            number=self.PR_NUMBER,
            head_branch=self.BRANCH,
            head=FakePRRef(sha="abc12345"),
        )
        defaults.update(kwargs)
        return FakePR(**defaults)

    def _seed_in_review(self, issue_number=50, *, pr=None, extra_state=None):
        gh = FakeGitHubClient()
        issue = make_issue(issue_number, label="in_review")
        gh.add_issue(issue)
        if pr is not None:
            gh.add_pr(pr)
        state = dict(
            branch=self.BRANCH,
            dev_agent="claude",
            dev_session_id="dev-sess",
            review_round=1,
        )
        if pr is not None:
            state["pr_number"] = pr.number
        if extra_state:
            state.update(extra_state)
        gh.seed_state(issue_number, **state)
        return gh, issue

    def test_pr_opened_event_on_fresh_pr_open(self) -> None:
        # _handle_implementing -> _on_commits opens a new PR and emits
        # `pr_opened` with the pr number and branch.
        gh = FakeGitHubClient()
        issue = make_issue(50, label="implementing")
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
        opened = self._events_of(gh, "pr_opened")
        self.assertEqual(len(opened), 1)
        ev = opened[0]
        self.assertEqual(ev["stage"], "implementing")
        self.assertEqual(ev["issue"], 50)
        self.assertEqual(ev["pr_number"], gh.opened_prs[0].number)
        self.assertEqual(ev["branch"], "orchestrator/issue-50")
        # `sha` carries the PR head sha from `pr.head.sha` so the audit
        # sink can correlate the open event with later merge / review IDs.
        self.assertEqual(ev["sha"], gh.opened_prs[0].head.sha)

    def test_pr_opened_not_emitted_when_reusing_existing_pr(self) -> None:
        # Recovery path: an existing open PR is reused rather than opened
        # again. The PR was already announced on its earlier tick, so no
        # `pr_opened` event should fire here.
        gh = FakeGitHubClient()
        issue = make_issue(51, label="implementing")
        gh.add_issue(issue)
        existing = FakePR(number=123, head_branch="orchestrator/issue-51")
        gh.existing_open_pr["orchestrator/issue-51"] = existing
        self._run(
            lambda: workflow._handle_implementing(gh, _TEST_SPEC, issue),
            run_agent=_agent(session_id="sess-1", last_message="implemented"),
            has_new_commits=[False, True],
            push_branch=True,
        )
        self.assertEqual(self._events_of(gh, "pr_opened"), [])

    def test_in_review_mergeable_does_not_emit_merge_events(self) -> None:
        # The orchestrator is manual-merge-only: a mergeable PR in_review
        # never produces a `merge_attempt` or orchestrator-initiated
        # `pr_merged` event. The HITL ping is observable instead.
        pr = self._open_pr(approved=True, mergeable=True, check_state="success")
        gh, issue = self._seed_in_review(pr=pr)

        self._run(
            lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
            run_agent=_agent(),
        )

        self.assertEqual(self._events_of(gh, "merge_attempt"), [])
        self.assertEqual(self._events_of(gh, "pr_merged"), [])
        # And no orchestrator-driven label flip to `done`.
        self.assertNotIn((50, "done"), gh.label_history)

    def test_pr_merged_event_on_external_merge_terminal(self) -> None:
        # A human (or another bot) merged the PR while we were in_review.
        # The terminal handler stamps `merged_at` and emits `pr_merged`
        # with `merge_method=external`.
        pr = self._open_pr(merged=True, state="closed")
        gh, issue = self._seed_in_review(
            pr=pr, extra_state={"conflict_round": 2},
        )
        self._run(
            lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
            run_agent=_agent(),
        )
        merged = self._events_of(gh, "pr_merged")
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["merge_method"], "external")
        self.assertEqual(merged[0]["pr_number"], self.PR_NUMBER)
        self.assertEqual(merged[0]["sha"], "abc12345")
        # In-review terminals carry the round counters from state so an
        # operator tailing the sink can attribute merges to the round count
        # that produced them, not just the issue number.
        self.assertEqual(merged[0]["review_round"], 1)
        self.assertEqual(merged[0]["conflict_round"], 2)
        # The orchestrator is permanently manual-merge-only and never
        # emits `merge_attempt` from in_review.
        self.assertEqual(self._events_of(gh, "merge_attempt"), [])

    def test_pr_closed_without_merge_event_on_terminal(self) -> None:
        pr = self._open_pr(merged=False, state="closed")
        gh, issue = self._seed_in_review(pr=pr)
        self._run(
            lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
            run_agent=_agent(),
        )
        closed = self._events_of(gh, "pr_closed_without_merge")
        self.assertEqual(len(closed), 1)
        self.assertEqual(closed[0]["stage"], "in_review")
        self.assertEqual(closed[0]["pr_number"], self.PR_NUMBER)

    def test_in_review_unmergeable_does_not_emit_conflict_round(self) -> None:
        # The orchestrator no longer routes from in_review to
        # `resolving_conflict` on an unmergeable gate. An unmergeable PR
        # parks awaiting human, so no `conflict_round` event is emitted
        # from this stage.
        pr = self._open_pr(approved=True, mergeable=False, check_state="success")
        gh, issue = self._seed_in_review(pr=pr)
        self._run(
            lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
            run_agent=_agent(),
        )
        self.assertEqual(self._events_of(gh, "conflict_round"), [])
        self.assertNotIn((50, "resolving_conflict"), gh.label_history)
        self.assertTrue(gh.pinned_data(50).get("awaiting_human"))


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
                issue = make_issue(20, label="implementing")
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
            self.assertTrue(gh.pinned_data(20).get("awaiting_human"))
            event_names = {e["event"] for e in gh.recorded_events}
            self.assertIn("agent_spawn", event_names)
            self.assertIn("agent_exit", event_names)
            self.assertIn("park_awaiting_human", event_names)
