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
from unittest.mock import patch

from orchestrator import config, workflow
from orchestrator.github import PinnedState
from orchestrator.usage import UsageMetrics

from tests.fakes import FakeGitHubClient, make_issue
from tests.workflow_helpers import (
    REVIEW_APPROVED_MESSAGE,
    _PatchedWorkflowMixin,
    _TEST_SPEC,
    _agent,
    _fake_worktree,
)
from tests.workflow_usage_test_support import _PoisonedThenFreshRun


_BACKEND_CLAUDE = "claude"
_COST_SOURCE_ESTIMATED = "estimated"
_COST_SOURCE_REPORTED = "reported"
_COST_SOURCE_UNKNOWN = "unknown-price"
_RUNS_KEY = "issue_agent_runs"
_TOKENS_KEY = "issue_total_tokens"
_COST_KEY = "issue_total_cost_usd"
_COST_SOURCES_KEY = "issue_cost_sources"
_RESUME_ISSUE_NUMBER = 940
_DEVELOPER_ISSUE_NUMBER = 30
_INTERRUPTED_DEVELOPER_ISSUE_NUMBER = 31
_REVIEWER_ISSUE_NUMBER = 32
_INTERRUPTED_REVIEWER_ISSUE_NUMBER = 33
_CLAUDE_OUTPUT_TOKENS = 50
_CLAUDE_CACHE_READ_TOKENS = 30
_CLAUDE_CACHE_WRITE_TOKENS = 20
_CLAUDE_RUN_COST = 1.5
_CLAUDE_TOTAL_TOKENS = 200
_CODEX_OUTPUT_TOKENS = 40
_CODEX_TOTAL_TOKENS = 140
_MULTI_RUN_COST = 2.0
_MULTI_RUN_TOKENS = 22
_MULTI_RUN_TOTAL_COST = 3.0


def _usage(**overrides) -> UsageMetrics:
    base = dict(backend=_BACKEND_CLAUDE, cost_source=_COST_SOURCE_ESTIMATED)
    base.update(overrides)
    return UsageMetrics(**base)


def _resume_seed():
    gh = FakeGitHubClient()
    issue = make_issue(_RESUME_ISSUE_NUMBER, label="resolving_conflict")
    gh.add_issue(issue)
    return gh, issue


class AccumulateIssueUsageHelperTest(unittest.TestCase):
    """The pure fold: token/cost math and the cost-source aggregate."""

    def test_single_fold_sums_and_records_source(self) -> None:
        state = PinnedState()
        workflow._accumulate_issue_usage(
            state,
            _usage(
                input_tokens=100,
                output_tokens=_CLAUDE_OUTPUT_TOKENS,
                cache_read_tokens=_CLAUDE_CACHE_READ_TOKENS,
                cache_write_tokens=_CLAUDE_CACHE_WRITE_TOKENS,
                cost_usd=_CLAUDE_RUN_COST,
                cost_source=_COST_SOURCE_ESTIMATED,
            ),
        )
        self.assertEqual(state.get(_RUNS_KEY), 1)
        self.assertEqual(state.get(_TOKENS_KEY), _CLAUDE_TOTAL_TOKENS)
        self.assertEqual(state.get(_COST_KEY), _CLAUDE_RUN_COST)
        self.assertEqual(state.get(_COST_SOURCES_KEY), [_COST_SOURCE_ESTIMATED])

    def test_codex_cached_tokens_excluded_from_total(self) -> None:
        # codex reports `input_tokens` as the whole prompt and `cached_tokens`
        # as the portion of it served from cache; summing the latter would
        # double-count part of the input.
        state = PinnedState()
        workflow._accumulate_issue_usage(
            state,
            _usage(
                backend="codex",
                input_tokens=100,
                output_tokens=_CODEX_OUTPUT_TOKENS,
                cached_tokens=60,
                cost_usd=0.5,
                cost_source=_COST_SOURCE_REPORTED,
            ),
        )
        self.assertEqual(state.get(_TOKENS_KEY), _CODEX_TOTAL_TOKENS)

    def test_runs_dedupe_sources_and_none_cost(
        self,
    ) -> None:
        usage_state = PinnedState()
        # A priced run, an unpriced run (cost None), and a second priced run
        # sharing the first's source.
        workflow._accumulate_issue_usage(
            usage_state,
            _usage(
                input_tokens=10,
                cost_usd=1.0,
                cost_source=_COST_SOURCE_ESTIMATED,
            ),
        )
        workflow._accumulate_issue_usage(
            usage_state,
            _usage(
                input_tokens=5,
                cost_usd=None,
                cost_source=_COST_SOURCE_UNKNOWN,
            ),
        )
        workflow._accumulate_issue_usage(
            usage_state,
            _usage(
                output_tokens=7,
                cost_usd=_MULTI_RUN_COST,
                cost_source=_COST_SOURCE_ESTIMATED,
            ),
        )
        self.assertEqual(usage_state.get(_RUNS_KEY), 3)
        self.assertEqual(usage_state.get(_TOKENS_KEY), _MULTI_RUN_TOKENS)
        # None-cost run contributes nothing to the dollar total.
        self.assertEqual(usage_state.get(_COST_KEY), _MULTI_RUN_TOTAL_COST)
        # Distinct sources, sorted, for the terminal verdict's (est.)/unknown.
        self.assertEqual(
            usage_state.get(_COST_SOURCES_KEY),
            [_COST_SOURCE_ESTIMATED, _COST_SOURCE_UNKNOWN],
        )

    def test_none_usage_is_noop(self) -> None:
        # Fail-open: a parse failure surfaces `result.usage is None`, which
        # must neither count a run nor create any counter key.
        empty = PinnedState()
        workflow._accumulate_issue_usage(empty, None)
        self.assertEqual(empty.data, {})
        # And it leaves existing counters untouched.
        seeded = PinnedState(data={_RUNS_KEY: 2, _TOKENS_KEY: 9})
        workflow._accumulate_issue_usage(seeded, None)
        self.assertEqual(seeded.get(_RUNS_KEY), 2)
        self.assertEqual(seeded.get(_TOKENS_KEY), 9)


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
                PinnedState(data={_RUNS_KEY: 0}),
            )
        )

    def test_verdict_slots_by_cost_source(self) -> None:
        cases = [
            # (sources, cost_usd) -> expected trailing cost slot
            ([_COST_SOURCE_REPORTED], 0.87, "$0.87"),
            ([_COST_SOURCE_ESTIMATED], 0.87, "$0.87 (est.)"),
            (
                [_COST_SOURCE_ESTIMATED, _COST_SOURCE_REPORTED],
                _CLAUDE_RUN_COST,
                "$1.50 (est.)",
            ),
            # An unpriced run collapses the whole figure to `unknown`, and
            # that dominates a co-present estimate (an unknown total is not
            # an estimate).
            ([_COST_SOURCE_UNKNOWN], None, "unknown"),
            ([_COST_SOURCE_ESTIMATED, _COST_SOURCE_UNKNOWN], 0.87, "unknown"),
            # A `no-usage` run priced nothing but blocks neither slot.
            (["no-usage"], None, "$0.00"),
        ]
        for sources, cost, expected_cost in cases:
            with self.subTest(sources=sources):
                verdict_data = {
                    _RUNS_KEY: 3,
                    _TOKENS_KEY: 45200,
                    _COST_SOURCES_KEY: sources,
                }
                if cost is not None:
                    verdict_data[_COST_KEY] = cost
                self.assertEqual(
                    workflow._format_issue_usage_verdict(
                        PinnedState(data=verdict_data),
                    ),
                    f":receipt: this issue: 3 agent runs · "
                    f"45,200 tokens · {expected_cost}",
                )

    def test_tokens_are_thousands_separated(self) -> None:
        line = workflow._format_issue_usage_verdict(
            PinnedState(data={
                _RUNS_KEY: 1,
                _TOKENS_KEY: 1234567,
                _COST_KEY: 12.3,
                _COST_SOURCES_KEY: [_COST_SOURCE_REPORTED],
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
        issue = make_issue(_DEVELOPER_ISSUE_NUMBER, label="implementing")
        gh.add_issue(issue)

        with patch.object(config, "DEV_AGENT", _BACKEND_CLAUDE):
            self._run(
                lambda: workflow._handle_implementing(gh, _TEST_SPEC, issue),
                run_agent=_agent(session_id="sess-fresh", last_message="done"),
                has_new_commits=[False, True],
                push_branch=True,
            )

        state = gh.pinned_data(_DEVELOPER_ISSUE_NUMBER)
        self.assertEqual(state[_RUNS_KEY], 1)
        # Empty stdout parses to a `no-usage` metric: a counted run with zero
        # tokens and no dollar cost.
        self.assertEqual(state[_TOKENS_KEY], 0)
        self.assertNotIn(_COST_KEY, state)
        self.assertEqual(state[_COST_SOURCES_KEY], ["no-usage"])

    def test_interrupted_spawn_keeps_counters_clear(self) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(
            _INTERRUPTED_DEVELOPER_ISSUE_NUMBER,
            label="implementing",
        )
        gh.add_issue(issue)

        with patch.object(config, "DEV_AGENT", _BACKEND_CLAUDE):
            self._run(
                lambda: workflow._handle_implementing(gh, _TEST_SPEC, issue),
                run_agent=_agent(
                    session_id="sess-x", last_message="", interrupted=True,
                ),
                has_new_commits=[False],
            )

        # The shutdown-sweep contract returns before `write_pinned_state`, so
        # the in-memory fold never reaches GitHub.
        state = gh.pinned_data(_INTERRUPTED_DEVELOPER_ISSUE_NUMBER)
        self.assertNotIn(_RUNS_KEY, state)
        self.assertNotIn(_TOKENS_KEY, state)


class ResumeRunUsageAccumulationTest(unittest.TestCase):
    """`_resume_dev_with_text` counts every real agent exit once: one for a
    plain resume, two when a poisoned resume triggers a fresh-spawn retry."""

    def test_plain_resume_counts_one_exit(self) -> None:
        gh, issue = _resume_seed()
        gh.seed_state(
            _RESUME_ISSUE_NUMBER,
            dev_agent=_BACKEND_CLAUDE,
            dev_session_id="live-sess",
            silent_park_count=0,
        )
        state = gh.read_pinned_state(issue)

        with patch.object(
            workflow, "_ensure_worktree", _fake_worktree,
        ), patch.object(
            workflow, "run_agent",
            lambda *agent_args, **agent_kwargs: _agent(
                session_id="live-sess",
                last_message="ok",
            ),
        ):
            workflow._resume_dev_with_text(gh, _TEST_SPEC, issue, state, "go")

        self.assertEqual(state.get(_RUNS_KEY), 1)

    def test_poisoned_retry_counts_both_exits(self) -> None:
        gh, issue = _resume_seed()
        gh.seed_state(
            _RESUME_ISSUE_NUMBER,
            dev_agent=_BACKEND_CLAUDE,
            dev_session_id="poisoned-sess",
            silent_park_count=0,
        )
        state = gh.read_pinned_state(issue)

        run_recorder = _PoisonedThenFreshRun()

        with patch.object(
            workflow, "_ensure_worktree", _fake_worktree,
        ), patch.object(workflow, "run_agent", run_recorder):
            workflow._resume_dev_with_text(gh, _TEST_SPEC, issue, state, "go")

        self.assertEqual(run_recorder.calls, ["poisoned-sess", None])
        # Both the poisoned resume and the fresh retry burned a real exit.
        self.assertEqual(state.get(_RUNS_KEY), 2)


class ReviewerRunUsageAccumulationTest(unittest.TestCase, _PatchedWorkflowMixin):
    """A validating reviewer run folds its usage into the same pinned state the
    approval handoff writes."""

    def test_reviewer_run_persists_one_run(self) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(_REVIEWER_ISSUE_NUMBER, label="validating")
        gh.add_issue(issue)
        gh.seed_state(
            _REVIEWER_ISSUE_NUMBER,
            pr_number=_REVIEWER_ISSUE_NUMBER,
            branch="orchestrator/geserdugarov__agent-orchestrator/issue-32",
            dev_agent=_BACKEND_CLAUDE,
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

        state = gh.pinned_data(_REVIEWER_ISSUE_NUMBER)
        self.assertEqual(state[_RUNS_KEY], 1)
        self.assertEqual(state[_TOKENS_KEY], 0)
        self.assertEqual(state[_COST_SOURCES_KEY], ["no-usage"])

    def test_interrupted_review_keeps_counters_clear(self) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(
            _INTERRUPTED_REVIEWER_ISSUE_NUMBER,
            label="validating",
        )
        gh.add_issue(issue)
        gh.seed_state(
            _INTERRUPTED_REVIEWER_ISSUE_NUMBER,
            pr_number=_INTERRUPTED_REVIEWER_ISSUE_NUMBER,
            branch="orchestrator/geserdugarov__agent-orchestrator/issue-33",
            dev_agent=_BACKEND_CLAUDE,
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
        state = gh.pinned_data(_INTERRUPTED_REVIEWER_ISSUE_NUMBER)
        self.assertNotIn(_RUNS_KEY, state)
        self.assertNotIn(_TOKENS_KEY, state)
        self.assertNotEqual(state.get("park_reason"), "reviewer_failed")
        self.assertFalse(state.get("awaiting_human"))


if __name__ == "__main__":
    unittest.main()
