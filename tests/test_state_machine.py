# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import json
import os
import pathlib
import re
import unittest
from unittest.mock import patch

os.environ.setdefault("ORCHESTRATOR_SKIP_DOTENV", "1")

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


class WorkflowLabelEnumTest(unittest.TestCase):
    """`WorkflowLabel` is a `StrEnum`: members ARE their wire strings, so
    every existing string comparison, JSON serialization, and frozenset
    membership keeps working unchanged."""

    def test_member_equals_its_wire_string(self) -> None:
        self.assertEqual(WorkflowLabel.VALIDATING, "validating")
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
        self.assertIn("validating", base_sync._PR_REFRESH_DETOUR_LABELS)
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
        # backlog / hold_base_sync are modifiers, not FSM states: they must
        # not leak into the workflow vocabulary.
        self.assertEqual(ControlLabel.BACKLOG, "backlog")
        self.assertNotIn(ControlLabel.BACKLOG, github.WORKFLOW_LABELS)
        self.assertNotIn(ControlLabel.HOLD_BASE_SYNC, github.WORKFLOW_LABELS)


class CoerceWorkflowLabelTest(unittest.TestCase):
    def test_valid_string_returns_member(self) -> None:
        self.assertIs(coerce_workflow_label("validating"), WorkflowLabel.VALIDATING)

    def test_member_is_idempotent(self) -> None:
        self.assertIs(
            coerce_workflow_label(WorkflowLabel.DONE), WorkflowLabel.DONE
        )

    def test_typo_raises_with_helpful_message(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            coerce_workflow_label("validatign")
        msg = str(ctx.exception)
        self.assertIn("validatign", msg)
        self.assertIn("valid workflow label", msg)


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

    def test_set_workflow_label_accepts_enum_and_string(self) -> None:
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
        result = gh.workflow_label(issue)
        self.assertIsInstance(result, WorkflowLabel)
        self.assertIs(result, WorkflowLabel.FIXING)

    def test_create_child_issue_rejects_typo(self) -> None:
        gh = FakeGitHubClient()
        with self.assertRaises(ValueError):
            gh.create_child_issue(
                title="t", body="b", parent_number=1, labels=["blokced"],
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
            for t in targets:
                self.assertIsInstance(t, WorkflowLabel)

    def test_question_has_no_inbound_edge(self) -> None:
        # `question` is operator-applied only; nothing transitions INTO it.
        for src, targets in ALLOWED_TRANSITIONS.items():
            self.assertNotIn(WorkflowLabel.QUESTION, targets, src)

    def test_entry_is_not_terminalizable(self) -> None:
        # An unlabeled issue only decomposes or implements -- never jumps
        # straight to done/rejected.
        self.assertEqual(
            ALLOWED_TRANSITIONS[None],
            frozenset({WorkflowLabel.DECOMPOSING, WorkflowLabel.IMPLEMENTING}),
        )

    def test_detour_set_matches_base_sync(self) -> None:
        # The explicit resolving_conflict sources must not drift from the
        # set the base-sync detour actually fires on.
        self.assertEqual(
            _DETOUR_TO_RESOLVING, base_sync._PR_REFRESH_DETOUR_LABELS,
        )

    def test_every_emitted_target_is_reachable(self) -> None:
        # Drift meta-test: every `set_workflow_label(..., WorkflowLabel.X)`
        # target in the package must be an allowed target somewhere in the
        # table, so a new write site can't outrun the declared graph.
        pkg = pathlib.Path(github.__file__).parent
        pat = re.compile(r"set_workflow_label\([^)]*?WorkflowLabel\.([A-Z_]+)")
        emitted: set[WorkflowLabel] = set()
        for py in pkg.rglob("*.py"):
            for m in pat.finditer(py.read_text()):
                emitted.add(WorkflowLabel[m.group(1)])
        reachable: set[WorkflowLabel] = set().union(*ALLOWED_TRANSITIONS.values())
        self.assertTrue(emitted, "scan found no set_workflow_label targets")
        self.assertLessEqual(emitted, reachable, emitted - reachable)


class IsAllowedTransitionTest(unittest.TestCase):
    def test_spine_edges_allowed(self) -> None:
        for cur, nxt in [
            (None, WorkflowLabel.DECOMPOSING),
            (None, WorkflowLabel.IMPLEMENTING),
            (WorkflowLabel.IMPLEMENTING, WorkflowLabel.VALIDATING),
            (WorkflowLabel.VALIDATING, WorkflowLabel.DOCUMENTING),
            (WorkflowLabel.DOCUMENTING, WorkflowLabel.IN_REVIEW),
            (WorkflowLabel.IN_REVIEW, WorkflowLabel.FIXING),
            (WorkflowLabel.FIXING, WorkflowLabel.VALIDATING),
            (WorkflowLabel.BLOCKED, WorkflowLabel.READY),
            (WorkflowLabel.BLOCKED, WorkflowLabel.DECOMPOSING),  # drift
            (WorkflowLabel.UMBRELLA, WorkflowLabel.DONE),
        ]:
            self.assertTrue(is_allowed_transition(cur, nxt), (cur, nxt))

    def test_illegal_edges_rejected(self) -> None:
        for cur, nxt in [
            (WorkflowLabel.VALIDATING, WorkflowLabel.IN_REVIEW),  # skips docs
            (WorkflowLabel.IMPLEMENTING, WorkflowLabel.DOCUMENTING),
            (WorkflowLabel.READY, WorkflowLabel.VALIDATING),  # skips implementing
            (None, WorkflowLabel.DONE),  # entry not terminalizable
        ]:
            self.assertFalse(is_allowed_transition(cur, nxt), (cur, nxt))

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

    def test_rejected_allowed_only_from_its_exact_sources(self) -> None:
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

    def test_question_finalizes_to_done_but_never_rejected(self) -> None:
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

    def test_resolving_conflict_only_from_detour_sources(self) -> None:
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


class GuardModeTest(unittest.TestCase):
    """`guard_transition` is the mode-aware wrapper `set_workflow_label`
    calls. `off` no-ops, `warn` logs+proceeds, `enforce` raises."""

    def test_off_never_raises_or_logs(self) -> None:
        with self.assertNoLogs("orchestrator.state_machine", level="WARNING"):
            guard_transition(
                WorkflowLabel.VALIDATING, WorkflowLabel.IN_REVIEW, "off",
            )

    def test_warn_logs_but_proceeds(self) -> None:
        with self.assertLogs("orchestrator.state_machine", level="WARNING") as cm:
            guard_transition(
                WorkflowLabel.VALIDATING, WorkflowLabel.IN_REVIEW, "warn",
            )
        self.assertTrue(
            any("illegal workflow transition" in m for m in cm.output), cm.output,
        )

    def test_enforce_raises_on_illegal(self) -> None:
        with self.assertRaises(IllegalTransition):
            guard_transition(
                WorkflowLabel.VALIDATING, WorkflowLabel.IN_REVIEW, "enforce",
            )

    def test_enforce_allows_legal(self) -> None:
        guard_transition(
            WorkflowLabel.VALIDATING, WorkflowLabel.DOCUMENTING, "enforce",
        )  # no raise

    def test_enforce_allows_same_label(self) -> None:
        guard_transition(
            WorkflowLabel.DONE, WorkflowLabel.DONE, "enforce",
        )  # no raise


class SetWorkflowLabelGuardWiringTest(unittest.TestCase):
    """The guard is wired through `set_workflow_label` (the single
    chokepoint), driven by `config.WORKFLOW_TRANSITION_GUARD`."""

    def _issue(self):
        gh = FakeGitHubClient()
        issue = make_issue(1, label="validating")
        gh.add_issue(issue)
        return gh, issue

    def test_enforce_blocks_illegal_relabel(self) -> None:
        gh, issue = self._issue()
        with patch.object(config, "WORKFLOW_TRANSITION_GUARD", "enforce"):
            with self.assertRaises(IllegalTransition):
                gh.set_workflow_label(issue, WorkflowLabel.IN_REVIEW)
        # Label unchanged after the rejected write.
        self.assertEqual(gh.workflow_label(issue), WorkflowLabel.VALIDATING)

    def test_warn_allows_illegal_relabel(self) -> None:
        gh, issue = self._issue()
        with patch.object(config, "WORKFLOW_TRANSITION_GUARD", "warn"):
            with self.assertLogs("orchestrator.state_machine", level="WARNING"):
                gh.set_workflow_label(issue, WorkflowLabel.IN_REVIEW)
        self.assertEqual(gh.workflow_label(issue), WorkflowLabel.IN_REVIEW)

    def test_enforce_allows_legal_relabel(self) -> None:
        gh, issue = self._issue()
        with patch.object(config, "WORKFLOW_TRANSITION_GUARD", "enforce"):
            gh.set_workflow_label(issue, WorkflowLabel.DOCUMENTING)
        self.assertEqual(gh.workflow_label(issue), WorkflowLabel.DOCUMENTING)


if __name__ == "__main__":
    unittest.main()
