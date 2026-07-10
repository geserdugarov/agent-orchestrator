# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Per-issue cumulative usage meter: the `_accumulate_issue_usage` fold, the
`_format_issue_usage_verdict` reader that renders the terminal receipt line,
and their wiring into the developer (implementing) and reviewer (validating)
run sites. Covers the token formula (codex `cached_tokens` excluded), the cost
/ cost-source aggregates, the `(est.)` / `unknown` verdict slots, and the
single-writer discipline that leaves an interrupted run's counters
unpersisted."""
from __future__ import annotations

import unittest
from typing import Optional
from unittest.mock import patch

from orchestrator import config, workflow
from orchestrator.github import PinnedState
from orchestrator.usage import UsageMetrics

from tests.fakes import FakeGitHubClient, make_issue
from tests.workflow_helpers import (
    REVIEW_APPROVED_MESSAGE,
    _FAKE_WT,
    _PatchedWorkflowMixin,
    _TEST_SPEC,
    _agent,
)


def _usage(**overrides) -> UsageMetrics:
    base = dict(backend="claude", cost_source="estimated")
    base.update(overrides)
    return UsageMetrics(**base)


class AccumulateIssueUsageHelperTest(unittest.TestCase):
    """The pure fold: token/cost math and the cost-source aggregate."""

    def test_single_fold_sums_and_records_source(self) -> None:
        state = PinnedState()
        workflow._accumulate_issue_usage(
            state,
            _usage(
                input_tokens=100, output_tokens=50,
                cache_read_tokens=30, cache_write_tokens=20,
                cost_usd=1.5, cost_source="estimated",
            ),
        )
        self.assertEqual(state.get("issue_agent_runs"), 1)
        self.assertEqual(state.get("issue_total_tokens"), 200)
        self.assertEqual(state.get("issue_total_cost_usd"), 1.5)
        self.assertEqual(state.get("issue_cost_sources"), ["estimated"])

    def test_codex_cached_tokens_excluded_from_total(self) -> None:
        # codex reports `input_tokens` as the whole prompt and `cached_tokens`
        # as the portion of it served from cache; summing the latter would
        # double-count part of the input.
        state = PinnedState()
        workflow._accumulate_issue_usage(
            state,
            _usage(
                backend="codex",
                input_tokens=100, output_tokens=40, cached_tokens=60,
                cost_usd=0.5, cost_source="reported",
            ),
        )
        self.assertEqual(state.get("issue_total_tokens"), 140)

    def test_runs_dedupe_sources_and_none_cost(
        self,
    ) -> None:
        usage_state = PinnedState()
        # A priced run, an unpriced run (cost None), and a second priced run
        # sharing the first's source.
        workflow._accumulate_issue_usage(
            usage_state, _usage(input_tokens=10, cost_usd=1.0, cost_source="estimated"),
        )
        workflow._accumulate_issue_usage(
            usage_state,
            _usage(input_tokens=5, cost_usd=None, cost_source="unknown-price"),
        )
        workflow._accumulate_issue_usage(
            usage_state, _usage(output_tokens=7, cost_usd=2.0, cost_source="estimated"),
        )
        self.assertEqual(usage_state.get("issue_agent_runs"), 3)
        self.assertEqual(usage_state.get("issue_total_tokens"), 22)
        # None-cost run contributes nothing to the dollar total.
        self.assertEqual(usage_state.get("issue_total_cost_usd"), 3.0)
        # Distinct sources, sorted, for the terminal verdict's (est.)/unknown.
        self.assertEqual(
            usage_state.get("issue_cost_sources"), ["estimated", "unknown-price"],
        )

    def test_none_usage_is_noop(self) -> None:
        # Fail-open: a parse failure surfaces `result.usage is None`, which
        # must neither count a run nor create any counter key.
        empty = PinnedState()
        workflow._accumulate_issue_usage(empty, None)
        self.assertEqual(empty.data, {})
        # And it leaves existing counters untouched.
        seeded = PinnedState(data={"issue_agent_runs": 2, "issue_total_tokens": 9})
        workflow._accumulate_issue_usage(seeded, None)
        self.assertEqual(seeded.get("issue_agent_runs"), 2)
        self.assertEqual(seeded.get("issue_total_tokens"), 9)


class FormatIssueUsageVerdictTest(unittest.TestCase):
    """The pure reader: `_format_issue_usage_verdict` renders the counters
    into the terminal receipt line, marking `(est.)` / `unknown` off the
    cost-source set and collapsing to None when nothing was counted."""

    def test_zero_runs_returns_none(self) -> None:
        # An empty state and an explicit zero both skip the line so a
        # terminal with no counted run posts no receipt.
        self.assertIsNone(workflow._format_issue_usage_verdict(PinnedState()))
        self.assertIsNone(
            workflow._format_issue_usage_verdict(
                PinnedState(data={"issue_agent_runs": 0}),
            )
        )

    def test_verdict_slots_by_cost_source(self) -> None:
        cases = [
            # (sources, cost_usd) -> expected trailing cost slot
            (["reported"], 0.87, "$0.87"),
            (["estimated"], 0.87, "$0.87 (est.)"),
            (["estimated", "reported"], 1.5, "$1.50 (est.)"),
            # An unpriced run collapses the whole figure to `unknown`, and
            # that dominates a co-present estimate (an unknown total is not
            # an estimate).
            (["unknown-price"], None, "unknown"),
            (["estimated", "unknown-price"], 0.87, "unknown"),
            # A `no-usage` run priced nothing but blocks neither slot.
            (["no-usage"], None, "$0.00"),
        ]
        for sources, cost, expected_cost in cases:
            with self.subTest(sources=sources):
                data = {
                    "issue_agent_runs": 3,
                    "issue_total_tokens": 45200,
                    "issue_cost_sources": sources,
                }
                if cost is not None:
                    data["issue_total_cost_usd"] = cost
                self.assertEqual(
                    workflow._format_issue_usage_verdict(PinnedState(data=data)),
                    f":receipt: this issue: 3 agent runs · "
                    f"45,200 tokens · {expected_cost}",
                )

    def test_tokens_are_thousands_separated(self) -> None:
        line = workflow._format_issue_usage_verdict(
            PinnedState(data={
                "issue_agent_runs": 1,
                "issue_total_tokens": 1234567,
                "issue_total_cost_usd": 12.3,
                "issue_cost_sources": ["reported"],
            })
        )
        self.assertEqual(
            line,
            ":receipt: this issue: 1 agent runs · 1,234,567 tokens · $12.30",
        )


class DeveloperRunUsageAccumulationTest(unittest.TestCase, _PatchedWorkflowMixin):
    """A fresh implementing spawn folds its usage into the pinned state the
    handler writes; an interrupted spawn returns without writing, so nothing
    accrues."""

    def test_fresh_spawn_persists_one_run(self) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(30, label="implementing")
        gh.add_issue(issue)

        with patch.object(config, "DEV_AGENT", "claude"):
            self._run(
                lambda: workflow._handle_implementing(gh, _TEST_SPEC, issue),
                run_agent=_agent(session_id="sess-fresh", last_message="done"),
                has_new_commits=[False, True],
                push_branch=True,
            )

        state = gh.pinned_data(30)
        self.assertEqual(state["issue_agent_runs"], 1)
        # Empty stdout parses to a `no-usage` metric: a counted run with zero
        # tokens and no dollar cost.
        self.assertEqual(state["issue_total_tokens"], 0)
        self.assertNotIn("issue_total_cost_usd", state)
        self.assertEqual(state["issue_cost_sources"], ["no-usage"])

    def test_interrupted_spawn_keeps_counters_clear(self) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(31, label="implementing")
        gh.add_issue(issue)

        with patch.object(config, "DEV_AGENT", "claude"):
            self._run(
                lambda: workflow._handle_implementing(gh, _TEST_SPEC, issue),
                run_agent=_agent(
                    session_id="sess-x", last_message="", interrupted=True,
                ),
                has_new_commits=[False],
            )

        # The shutdown-sweep contract returns before `write_pinned_state`, so
        # the in-memory fold never reaches GitHub.
        state = gh.pinned_data(31)
        self.assertNotIn("issue_agent_runs", state)
        self.assertNotIn("issue_total_tokens", state)


class ResumeRunUsageAccumulationTest(unittest.TestCase):
    """`_resume_dev_with_text` counts every real agent exit once: one for a
    plain resume, two when a poisoned resume triggers a fresh-spawn retry."""

    def _seeded(self):
        gh = FakeGitHubClient()
        issue = make_issue(940, label="resolving_conflict")
        gh.add_issue(issue)
        return gh, issue

    def test_plain_resume_counts_one_exit(self) -> None:
        gh, issue = self._seeded()
        gh.seed_state(
            940, dev_agent="claude", dev_session_id="live-sess",
            silent_park_count=0,
        )
        state = gh.read_pinned_state(issue)

        with patch.object(
            workflow, "_ensure_worktree", lambda spec, n, **_: _FAKE_WT
        ), patch.object(
            workflow, "run_agent",
            lambda *a, **k: _agent(session_id="live-sess", last_message="ok"),
        ):
            workflow._resume_dev_with_text(gh, _TEST_SPEC, issue, state, "go")

        self.assertEqual(state.get("issue_agent_runs"), 1)

    def test_poisoned_retry_counts_both_exits(self) -> None:
        gh, issue = self._seeded()
        gh.seed_state(
            940, dev_agent="claude", dev_session_id="poisoned-sess",
            silent_park_count=0,
        )
        state = gh.read_pinned_state(issue)

        calls: list[Optional[str]] = []

        def fake_run(agent, prompt, wt, *, resume_session_id=None, extra_args=()):
            calls.append(resume_session_id)
            if resume_session_id == "poisoned-sess":
                return _agent(
                    session_id="", last_message="",
                    stderr="Error: No conversation found with session ID: x",
                )
            return _agent(session_id="fresh-sess", last_message="ok")

        with patch.object(
            workflow, "_ensure_worktree", lambda spec, n, **_: _FAKE_WT
        ), patch.object(workflow, "run_agent", fake_run):
            workflow._resume_dev_with_text(gh, _TEST_SPEC, issue, state, "go")

        self.assertEqual(calls, ["poisoned-sess", None])
        # Both the poisoned resume and the fresh retry burned a real exit.
        self.assertEqual(state.get("issue_agent_runs"), 2)


class ReviewerRunUsageAccumulationTest(unittest.TestCase, _PatchedWorkflowMixin):
    """A validating reviewer run folds its usage into the same pinned state the
    approval handoff writes."""

    def test_reviewer_run_persists_one_run(self) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(32, label="validating")
        gh.add_issue(issue)
        gh.seed_state(
            32,
            pr_number=32,
            branch="orchestrator/geserdugarov__agent-orchestrator/issue-32",
            dev_agent="claude",
            dev_session_id="dev-sess",
            review_round=0,
        )

        with patch.object(config, "REVIEW_AGENT", "codex"):
            self._run(
                lambda: workflow._handle_validating(gh, _TEST_SPEC, issue),
                run_agent=_agent(
                    session_id="rev-sess",
                    last_message=REVIEW_APPROVED_MESSAGE,
                ),
            )

        state = gh.pinned_data(32)
        self.assertEqual(state["issue_agent_runs"], 1)
        self.assertEqual(state["issue_total_tokens"], 0)
        self.assertEqual(state["issue_cost_sources"], ["no-usage"])

    def test_interrupted_review_keeps_counters_clear(self) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(33, label="validating")
        gh.add_issue(issue)
        gh.seed_state(
            33,
            pr_number=33,
            branch="orchestrator/geserdugarov__agent-orchestrator/issue-33",
            dev_agent="claude",
            dev_session_id="dev-sess",
            review_round=0,
            # Seed the drift baseline so `_detect_user_content_change` does not
            # itself write pinned state on first encounter -- this test asserts
            # the handler writes NOTHING once the reviewer run is interrupted.
            user_content_hash=workflow._compute_user_content_hash(issue, set()),
        )

        with patch.object(config, "REVIEW_AGENT", "codex"):
            self._run(
                lambda: workflow._handle_validating(gh, _TEST_SPEC, issue),
                run_agent=_agent(
                    session_id="", last_message="", exit_code=1,
                    interrupted=True,
                ),
            )

        # A shutdown-killed reviewer returns before `write_pinned_state`, so
        # neither the folded counters nor a `reviewer_failed` park reach GitHub.
        state = gh.pinned_data(33)
        self.assertNotIn("issue_agent_runs", state)
        self.assertNotIn("issue_total_tokens", state)
        self.assertNotEqual(state.get("park_reason"), "reviewer_failed")
        self.assertFalse(state.get("awaiting_human"))


if __name__ == "__main__":
    unittest.main()
