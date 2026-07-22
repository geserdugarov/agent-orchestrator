# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import json
import pathlib
import re
import unittest
from unittest.mock import MagicMock, patch

from orchestrator import base_sync, config, github, workflow
from orchestrator.state_machine import (
    ALLOWED_TRANSITIONS,
    ControlLabel,
    IllegalTransition,
    WorkflowLabel,
    _DETOUR_TO_RESOLVING,
    coerce_workflow_label,
    guard_transition,
    is_allowed_transition,
)

from tests.fakes import FakeGitHubClient, make_issue


_VALIDATING_LABEL = "validating"
_GUARD_ENFORCE = "enforce"
_GUARD_CONFIG_NAME = "WORKFLOW_TRANSITION_GUARD"


def _guarded_issue():
    gh = FakeGitHubClient()
    issue = make_issue(1, label=_VALIDATING_LABEL)
    gh.add_issue(issue)
    return gh, issue


def _coerce_error(label_value: str) -> ValueError:
    try:
        coerce_workflow_label(label_value)
    except ValueError as error:
        return error
    raise AssertionError("coerce_workflow_label did not reject the value")


class WorkflowLabelEnumTest(unittest.TestCase):
    """`WorkflowLabel` is a `StrEnum`: members ARE their wire strings, so
    every existing string comparison, JSON serialization, and frozenset
    membership keeps working unchanged."""

    def test_member_equals_its_wire_string(self) -> None:
        self.assertEqual(WorkflowLabel.VALIDATING, _VALIDATING_LABEL)
        self.assertEqual(WorkflowLabel.IN_REVIEW, "in_review")
        self.assertTrue(WorkflowLabel.DONE == "done")

    def test_json_serializes_as_plain_string(self) -> None:
        payload = {"label": WorkflowLabel.BLOCKED}
        self.assertEqual(json.dumps(payload), '{"label": "blocked"}')

    def test_frozenset_membership_both_directions(self) -> None:
        # Plain string against an enum-valued set, and enum against a
        # string-seeded set -- both must hold (hash/eq match str).
        self.assertIn("blocked", workflow._FAMILY_AWARE_LABELS)
        self.assertIn(WorkflowLabel.BLOCKED, workflow._FAMILY_AWARE_LABELS)
        self.assertIn(_VALIDATING_LABEL, base_sync._PR_REFRESH_DETOUR_LABELS)
        self.assertIn(WorkflowLabel.FIXING, base_sync._PR_REFRESH_DETOUR_LABELS)

    def test_workflow_labels_frozenset_is_the_enum(self) -> None:
        self.assertEqual(github.WORKFLOW_LABELS, frozenset(WorkflowLabel))
        self.assertIn("question", github.WORKFLOW_LABELS)

    def test_spec_table_is_exhaustive(self) -> None:
        self.assertEqual(
            {spec[0] for spec in github.WORKFLOW_LABEL_SPECS},
            set(WorkflowLabel),
        )

    def test_control_labels_are_not_workflow_states(self) -> None:
        # Control labels are modifiers, not FSM states: they must not leak
        # into the workflow vocabulary.
        self.assertEqual(ControlLabel.BACKLOG, "backlog")
        self.assertEqual(ControlLabel.PAUSED, "paused")
        self.assertEqual(
            ControlLabel.COMMUNITY_CONTRIBUTION, "community_contribution",
        )
        for label in ControlLabel:
            self.assertNotIn(label, github.WORKFLOW_LABELS)

    def test_control_label_specs_are_exhaustive(self) -> None:
        self.assertEqual(
            {spec[0] for spec in github.CONTROL_LABEL_SPECS},
            set(ControlLabel),
        )
        # `community_contribution` is registered for bootstrap but is not a
        # hard skip: it coexists with the workflow rather than pausing it.
        self.assertNotIn(
            github.COMMUNITY_CONTRIBUTION_LABEL, github.HARD_SKIP_CONTROL_LABELS
        )


class CoerceWorkflowLabelTest(unittest.TestCase):
    def test_valid_string_returns_member(self) -> None:
        self.assertIs(
            coerce_workflow_label(_VALIDATING_LABEL),
            WorkflowLabel.VALIDATING,
        )

    def test_member_is_idempotent(self) -> None:
        self.assertIs(
            coerce_workflow_label(WorkflowLabel.DONE), WorkflowLabel.DONE
        )

    def test_typo_raises_with_helpful_message(self) -> None:
        msg = str(_coerce_error("validatign"))
        self.assertIn("validatign", msg)
        self.assertIn("valid workflow label", msg)

    def test_accepts_value_keyword(self) -> None:
        # `value` is the public keyword; callers may pass the label by name.
        self.assertIs(
            coerce_workflow_label(value=_VALIDATING_LABEL),
            WorkflowLabel.VALIDATING,
        )


class LabelWriteTypoGuardTest(unittest.TestCase):
    """Every orchestrator-authored workflow-label write coerces, so a
    typo raises instead of applying an invisible label. The fake mirrors
    the real client, so the whole fake-backed suite shares the guard."""

    def test_set_workflow_label_rejects_typo(self) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(1, label="implementing")
        gh.add_issue(issue)
        with self.assertRaises(ValueError):
            gh.set_workflow_label(issue, "vaildating")

    def test_set_label_accepts_enum_and_string(self) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(1, label="implementing")
        gh.add_issue(issue)
        gh.set_workflow_label(issue, WorkflowLabel.VALIDATING)
        self.assertEqual(gh.workflow_label(issue), WorkflowLabel.VALIDATING)
        gh.set_workflow_label(issue, "documenting")  # plain string still ok
        self.assertEqual(gh.workflow_label(issue), WorkflowLabel.DOCUMENTING)

    def test_workflow_label_returns_typed_member(self) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(1, label="fixing")
        gh.add_issue(issue)
        workflow_label = gh.workflow_label(issue)
        self.assertIsInstance(workflow_label, WorkflowLabel)
        self.assertIs(workflow_label, WorkflowLabel.FIXING)

    def test_create_child_rejects_control_label(self) -> None:
        # A child is born with a workflow label only: a misspelling and any
        # control label (never seeded at creation) both raise here.
        gh = FakeGitHubClient()
        for label in ("blokced", "backlog"):
            with self.subTest(label=label):
                with self.assertRaises(ValueError):
                    gh.create_child_issue(
                        title="t", body="b", parent_number=1, labels=[label],
                    )


class TransitionTableTest(unittest.TestCase):
    """`ALLOWED_TRANSITIONS` is the declared, enforced state graph."""

    def test_keys_cover_every_state_plus_entry(self) -> None:
        self.assertEqual(
            set(ALLOWED_TRANSITIONS), {None} | set(WorkflowLabel),
        )

    def test_terminals_have_no_outgoing_edges(self) -> None:
        self.assertEqual(ALLOWED_TRANSITIONS[WorkflowLabel.DONE], frozenset())
        self.assertEqual(ALLOWED_TRANSITIONS[WorkflowLabel.REJECTED], frozenset())

    def test_every_target_is_a_workflow_label(self) -> None:
        for targets in ALLOWED_TRANSITIONS.values():
            for target in targets:
                self.assertIsInstance(target, WorkflowLabel)

    def test_question_has_no_inbound_edge(self) -> None:
        # `question` is operator-applied only; nothing transitions INTO it.
        for source, targets in ALLOWED_TRANSITIONS.items():
            self.assertNotIn(WorkflowLabel.QUESTION, targets, source)

    def test_entry_is_not_terminalizable(self) -> None:
        # An unlabeled issue only decomposes or implements -- never jumps
        # straight to done/rejected.
        self.assertEqual(
            ALLOWED_TRANSITIONS[None],
            frozenset((WorkflowLabel.DECOMPOSING, WorkflowLabel.IMPLEMENTING)),
        )

    def test_detour_set_matches_base_sync(self) -> None:
        # The explicit resolving_conflict sources must not drift from the
        # set the base-sync detour actually fires on.
        self.assertEqual(
            _DETOUR_TO_RESOLVING, base_sync._PR_REFRESH_DETOUR_LABELS,
        )


class TransitionGraphReachabilityTest(unittest.TestCase):
    """Every declared state participates in the live workflow graph."""

    def test_every_emitted_target_is_reachable(self) -> None:
        # Drift meta-test: every `set_workflow_label(..., WorkflowLabel.X)`
        # target in the package must be an allowed target somewhere in the
        # table, so a new write site can't outrun the declared graph.
        package = pathlib.Path(github.__file__).parent
        pattern = re.compile(r"set_workflow_label\([^)]*?WorkflowLabel\.([A-Z_]+)")
        emitted: set[WorkflowLabel] = set()
        for py_file in package.rglob("*.py"):
            for match in pattern.finditer(py_file.read_text()):
                emitted.add(WorkflowLabel[match.group(1)])
        reachable: set[WorkflowLabel] = set().union(*ALLOWED_TRANSITIONS.values())
        self.assertTrue(emitted, "scan found no set_workflow_label targets")
        self.assertLessEqual(emitted, reachable, emitted - reachable)

    def test_every_state_reachable_from_entry(self) -> None:
        # Global forward-reachability BFS from the real entry frontier -- the
        # invariant `test_every_emitted_target_is_reachable` cannot enforce.
        # That check is 1-hop set membership: a target passes as long as it is
        # *somebody's* target, so an orphaned island (e.g. a future `{X -> Y,
        # Y -> X}` neither of which the entry can reach) still passes because
        # each is the other's target. A true BFS from the entry rejects it.
        # `question` is operator-applied only and has no inbound edge (see
        # `test_question_has_no_inbound_edge`), so it is seeded as already-seen;
        # every other state must be reached from the `None` unlabeled entry.
        seen = {WorkflowLabel.QUESTION}
        frontier: list[WorkflowLabel | None] = [None, WorkflowLabel.QUESTION]
        while frontier:
            state = frontier.pop()
            for target in ALLOWED_TRANSITIONS.get(state, frozenset()):
                if target not in seen:
                    seen.add(target)
                    frontier.append(target)
        self.assertEqual(set(WorkflowLabel) - seen, set())

    def test_every_nonterminal_reaches_a_terminal(self) -> None:
        # Terminal-liveness BFS on the reversed graph from the terminals: every
        # non-terminal must have a path to `done`/`rejected`, so no edit can
        # introduce a non-terminal sink or an exit-less cycle an issue could
        # enter and never leave toward a terminal. The `None` pseudo-entry is
        # not a real state, so it is skipped rather than required to co-reach.
        terminals = {WorkflowLabel.DONE, WorkflowLabel.REJECTED}
        reverse: dict[WorkflowLabel, set[WorkflowLabel | None]] = {}
        for source, targets in ALLOWED_TRANSITIONS.items():
            for target in targets:
                reverse.setdefault(target, set()).add(source)
        seen = set(terminals)
        frontier = list(terminals)
        while frontier:
            state = frontier.pop()
            for pred in reverse.get(state, set()):
                if pred is not None and pred not in seen:
                    seen.add(pred)
                    frontier.append(pred)
        self.assertEqual(set(WorkflowLabel) - seen, set())


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


if __name__ == "__main__":
    unittest.main()
