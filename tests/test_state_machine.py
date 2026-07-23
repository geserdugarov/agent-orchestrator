# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import json
import pathlib
import re
import unittest

from orchestrator import base_sync, github, workflow
from orchestrator.state_machine import (
    ALLOWED_TRANSITIONS,
    ControlLabel,
    WorkflowLabel,
    _DETOUR_TO_RESOLVING,
    coerce_workflow_label,
)

from tests.fakes import FakeGitHubClient, make_issue


_VALIDATING_LABEL = "validating"
_LABEL_WRITE_PATTERN = re.compile(
    r"set_workflow_label\([^)]*?WorkflowLabel\.([A-Z_]+)",
)


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
        emitted: set[WorkflowLabel] = set()
        for py_file in package.rglob("*.py"):
            for match in _LABEL_WRITE_PATTERN.finditer(py_file.read_text()):
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
        seen = self._walk_reverse(
            self._reverse_transitions(),
            terminals,
        )
        self.assertEqual(set(WorkflowLabel) - seen, set())

    def _reverse_transitions(
        self,
    ) -> dict[WorkflowLabel, set[WorkflowLabel | None]]:
        reverse: dict[WorkflowLabel, set[WorkflowLabel | None]] = {}
        for source, targets in ALLOWED_TRANSITIONS.items():
            for target in targets:
                reverse.setdefault(target, set()).add(source)
        return reverse

    def _walk_reverse(
        self,
        reverse: dict[WorkflowLabel, set[WorkflowLabel | None]],
        terminals: set[WorkflowLabel],
    ) -> set[WorkflowLabel]:
        seen = set(terminals)
        frontier = list(terminals)
        while frontier:
            state = frontier.pop()
            for predecessor in reverse.get(state, set()):
                if predecessor is not None and predecessor not in seen:
                    seen.add(predecessor)
                    frontier.append(predecessor)
        return seen


if __name__ == "__main__":
    unittest.main()
