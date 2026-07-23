# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Transition decisions, terminal edges, and guard wiring."""
from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from orchestrator import config
from orchestrator.state_machine import (
    IllegalTransition,
    WorkflowLabel,
    guard_transition,
    is_allowed_transition,
)

from tests.fakes import FakeGitHubClient, make_issue


_VALIDATING_LABEL = "validating"
_GUARD_ENFORCE = "enforce"
_GUARD_CONFIG_NAME = "WORKFLOW_TRANSITION_GUARD"


def _guarded_issue():
    github = FakeGitHubClient()
    issue = make_issue(1, label=_VALIDATING_LABEL)
    github.add_issue(issue)
    return github, issue


class IsAllowedTransitionTest(unittest.TestCase):
    def test_spine_edges_allowed(self) -> None:
        for cur, nxt in (
            (None, WorkflowLabel.DECOMPOSING),
            (None, WorkflowLabel.IMPLEMENTING),
            (WorkflowLabel.IMPLEMENTING, WorkflowLabel.VALIDATING),
            (WorkflowLabel.VALIDATING, WorkflowLabel.DOCUMENTING),
            (WorkflowLabel.VALIDATING, WorkflowLabel.FIXING),
            (WorkflowLabel.DOCUMENTING, WorkflowLabel.IN_REVIEW),
            (WorkflowLabel.IN_REVIEW, WorkflowLabel.FIXING),
            (WorkflowLabel.FIXING, WorkflowLabel.VALIDATING),
            (WorkflowLabel.BLOCKED, WorkflowLabel.READY),
            (WorkflowLabel.BLOCKED, WorkflowLabel.DECOMPOSING),  # drift
            (WorkflowLabel.UMBRELLA, WorkflowLabel.DONE),
        ):
            self.assertTrue(is_allowed_transition(cur, nxt), (cur, nxt))

    def test_illegal_edges_rejected(self) -> None:
        for cur, nxt in (
            (WorkflowLabel.VALIDATING, WorkflowLabel.IN_REVIEW),  # skips docs
            (WorkflowLabel.IMPLEMENTING, WorkflowLabel.IN_REVIEW),  # skips the reviewer path
            (WorkflowLabel.IMPLEMENTING, WorkflowLabel.DOCUMENTING),
            (WorkflowLabel.READY, WorkflowLabel.VALIDATING),  # skips implementing
            (None, WorkflowLabel.DONE),  # entry not terminalizable
        ):
            self.assertFalse(is_allowed_transition(cur, nxt), (cur, nxt))

    def test_conflict_only_from_detour_sources(self) -> None:
        self.assertTrue(
            is_allowed_transition(
                WorkflowLabel.VALIDATING, WorkflowLabel.RESOLVING_CONFLICT,
            )
        )
        # `ready` is not a PR-having detour source.
        self.assertFalse(
            is_allowed_transition(
                WorkflowLabel.READY, WorkflowLabel.RESOLVING_CONFLICT,
            )
        )

    def test_same_label_is_allowed(self) -> None:
        # Idempotent re-set, even on a terminal.
        self.assertTrue(
            is_allowed_transition(WorkflowLabel.DONE, WorkflowLabel.DONE)
        )
        self.assertTrue(
            is_allowed_transition(
                WorkflowLabel.VALIDATING, WorkflowLabel.VALIDATING,
            )
        )


class TerminalTransitionTest(unittest.TestCase):
    """Terminal transitions are limited to their exact workflow sources."""

    def test_done_allowed_only_from_its_exact_sources(self) -> None:
        # External-merge / drain sources, plus umbrella/question whose own
        # forward completion is `-> done`. NOT the pre-PR states.
        sources = {
            WorkflowLabel.IMPLEMENTING, WorkflowLabel.VALIDATING,
            WorkflowLabel.DOCUMENTING, WorkflowLabel.IN_REVIEW,
            WorkflowLabel.FIXING, WorkflowLabel.RESOLVING_CONFLICT,
            WorkflowLabel.UMBRELLA, WorkflowLabel.QUESTION,
        }
        for state in WorkflowLabel:
            if state in (WorkflowLabel.DONE, WorkflowLabel.REJECTED):
                continue
            self.assertEqual(
                is_allowed_transition(state, WorkflowLabel.DONE),
                state in sources, state,
            )

    def test_rejected_only_from_exact_sources(self) -> None:
        sources = {
            WorkflowLabel.IMPLEMENTING, WorkflowLabel.VALIDATING,
            WorkflowLabel.DOCUMENTING, WorkflowLabel.IN_REVIEW,
            WorkflowLabel.FIXING, WorkflowLabel.RESOLVING_CONFLICT,
        }
        for state in WorkflowLabel:
            if state in (WorkflowLabel.DONE, WorkflowLabel.REJECTED):
                continue
            self.assertEqual(
                is_allowed_transition(state, WorkflowLabel.REJECTED),
                state in sources, state,
            )

    def test_question_can_finish_but_not_reject(self) -> None:
        # Maximal-exactness: `question` only finalizes to `done`; nothing
        # writes `question -> rejected`, so it must be illegal.
        self.assertTrue(
            is_allowed_transition(WorkflowLabel.QUESTION, WorkflowLabel.DONE)
        )
        self.assertFalse(
            is_allowed_transition(WorkflowLabel.QUESTION, WorkflowLabel.REJECTED)
        )

    def test_pre_pr_states_are_not_terminalizable(self) -> None:
        # decomposing / ready / blocked have no PR and no terminal writer.
        for state in (
            WorkflowLabel.DECOMPOSING, WorkflowLabel.READY, WorkflowLabel.BLOCKED,
        ):
            self.assertFalse(
                is_allowed_transition(state, WorkflowLabel.DONE), state,
            )
            self.assertFalse(
                is_allowed_transition(state, WorkflowLabel.REJECTED), state,
            )


class GuardModeTest(unittest.TestCase):
    """`guard_transition` is the mode-aware wrapper `set_workflow_label`
    calls. `off` no-ops, `warn` logs+proceeds, `enforce` raises."""

    def test_off_never_raises_or_logs(self) -> None:
        with self.assertNoLogs("orchestrator.state_machine", level="WARNING"):
            guard_transition(
                WorkflowLabel.VALIDATING, WorkflowLabel.IN_REVIEW, "off",
            )

    def test_warn_logs_but_proceeds(self) -> None:
        warning_mock = MagicMock()
        with patch(
            "orchestrator.state_machine.log.warning",
            warning_mock,
        ):
            guard_transition(
                WorkflowLabel.VALIDATING, WorkflowLabel.IN_REVIEW, "warn",
            )
        warning_mock.assert_called_once()
        message, *args = warning_mock.call_args.args
        self.assertIn(
            "illegal workflow transition",
            message % tuple(args),
        )

    def test_enforce_raises_on_illegal(self) -> None:
        with self.assertRaises(IllegalTransition):
            guard_transition(
                WorkflowLabel.VALIDATING, WorkflowLabel.IN_REVIEW, _GUARD_ENFORCE,
            )

    def test_enforce_allows_legal(self) -> None:
        guard_transition(
            WorkflowLabel.VALIDATING, WorkflowLabel.DOCUMENTING, _GUARD_ENFORCE,
        )  # no raise

    def test_enforce_allows_same_label(self) -> None:
        guard_transition(
            WorkflowLabel.DONE, WorkflowLabel.DONE, _GUARD_ENFORCE,
        )  # no raise


class SetWorkflowLabelGuardWiringTest(unittest.TestCase):
    """The guard is wired through `set_workflow_label` (the single
    chokepoint), driven by `config.WORKFLOW_TRANSITION_GUARD`."""

    def test_enforce_blocks_illegal_relabel(self) -> None:
        gh, issue = _guarded_issue()
        with patch.object(config, _GUARD_CONFIG_NAME, _GUARD_ENFORCE):
            with self.assertRaises(IllegalTransition):
                gh.set_workflow_label(issue, WorkflowLabel.IN_REVIEW)
        # Label unchanged after the rejected write.
        self.assertEqual(gh.workflow_label(issue), WorkflowLabel.VALIDATING)

    def test_warn_allows_illegal_relabel(self) -> None:
        gh, issue = _guarded_issue()
        with patch.object(config, _GUARD_CONFIG_NAME, "warn"):
            with self.assertLogs("orchestrator.state_machine", level="WARNING"):
                gh.set_workflow_label(issue, WorkflowLabel.IN_REVIEW)
        self.assertEqual(gh.workflow_label(issue), WorkflowLabel.IN_REVIEW)

    def test_enforce_allows_legal_relabel(self) -> None:
        gh, issue = _guarded_issue()
        with patch.object(config, _GUARD_CONFIG_NAME, _GUARD_ENFORCE):
            gh.set_workflow_label(issue, WorkflowLabel.DOCUMENTING)
        self.assertEqual(gh.workflow_label(issue), WorkflowLabel.DOCUMENTING)

    def test_enforce_allows_validation_fix_loop(self) -> None:
        gh, issue = _guarded_issue()
        with patch.object(config, _GUARD_CONFIG_NAME, _GUARD_ENFORCE):
            gh.set_workflow_label(issue, WorkflowLabel.FIXING)
        self.assertEqual(gh.workflow_label(issue), WorkflowLabel.FIXING)
