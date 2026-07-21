# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Verdict parsers used by review and documentation stages: marker shape,
case insensitivity, last-marker-wins semantics, and the strict rules that
keep ambiguous prose from being misread as a structured outcome.

The parsers live in `orchestrator.workflow_messages`; this suite imports them
from there (the module that owns the behavior) and separately pins the
historical `orchestrator.workflow._parse_*` re-export as a compatibility
contract."""
from __future__ import annotations

import unittest

from orchestrator import workflow
from orchestrator.workflow_messages import (
    _parse_documentation_verdict,
    _parse_review_verdict,
)

from tests.workflow_helpers import (
    VERDICT_APPROVED,
    VERDICT_CHANGES_REQUESTED,
    VERDICT_UNKNOWN,
)


REVIEW_APPROVED_BODY = "Looks good."
REVIEW_APPROVED_MARKER = "VERDICT: APPROVED"
REVIEW_CHANGES_REQUESTED_MARKER = "VERDICT: CHANGES_REQUESTED"
DOCS_NO_CHANGE = "no_change"
DOCS_NO_CHANGE_MARKER = "DOCS: NO_CHANGE"


class ParseReviewVerdictTest(unittest.TestCase):
    def test_approved_alone_on_line(self) -> None:
        self.assertEqual(
            _parse_review_verdict(
                f"{REVIEW_APPROVED_BODY}\n\n{REVIEW_APPROVED_MARKER}"
            ),
            (VERDICT_APPROVED, REVIEW_APPROVED_BODY),
        )

    def test_changes_requested_with_numbered_list(self) -> None:
        msg = (
            "1. Fix typo in README\n2. Add a test for the empty case\n\n"
            f"{REVIEW_CHANGES_REQUESTED_MARKER}"
        )
        verdict, body = _parse_review_verdict(msg)
        self.assertEqual(verdict, VERDICT_CHANGES_REQUESTED)
        self.assertIn("1. Fix typo in README", body)
        self.assertNotIn("VERDICT", body)

    def test_inline_marker_is_accepted(self) -> None:
        self.assertEqual(
            _parse_review_verdict(f"All good. {REVIEW_APPROVED_MARKER}"),
            (VERDICT_APPROVED, "All good."),
        )

    def test_case_insensitive(self) -> None:
        verdict, _ = _parse_review_verdict("verdict: approved")
        self.assertEqual(verdict, VERDICT_APPROVED)

    def test_last_marker_wins(self) -> None:
        msg = (
            f"I considered {REVIEW_APPROVED_MARKER} but a test fails.\n"
            f"{REVIEW_CHANGES_REQUESTED_MARKER}"
        )
        verdict, _ = _parse_review_verdict(msg)
        self.assertEqual(verdict, VERDICT_CHANGES_REQUESTED)

    def test_no_marker_returns_unknown(self) -> None:
        self.assertEqual(
            _parse_review_verdict("looks fine to me"),
            (VERDICT_UNKNOWN, "looks fine to me"),
        )

    def test_empty_message_returns_unknown(self) -> None:
        self.assertEqual(_parse_review_verdict(""), (VERDICT_UNKNOWN, ""))


class ParseDocumentationVerdictTest(unittest.TestCase):
    """Documentation stage outputs one of three observable outcomes:

      * Valid 'updated' -- the agent committed a `docs:` change. The
        parser does NOT see this; the stage handler detects it from the
        new commit. The case here is that a message describing the
        update but lacking the no-change marker must still return
        'unknown' so a forgotten commit can't be misread as no-change.
      * Valid 'no_change' -- the explicit `DOCS: NO_CHANGE` marker.
      * Invalid -- ambiguous text without the marker, including
        plausible-but-unstructured 'no changes needed' phrasing that
        must NOT be accepted as success.
    """

    def test_no_change_marker_alone_on_line(self) -> None:
        self.assertEqual(
            _parse_documentation_verdict(
                "Diff is internal-only; nothing user-visible changed."
                f"\n\n{DOCS_NO_CHANGE_MARKER}"
            ),
            (DOCS_NO_CHANGE, "Diff is internal-only; nothing user-visible changed."),
        )

    def test_no_change_marker_case_insensitive(self) -> None:
        verdict, _ = _parse_documentation_verdict("docs: no_change")
        self.assertEqual(verdict, DOCS_NO_CHANGE)

    def test_last_marker_wins(self) -> None:
        # Mirrors `_parse_review_verdict`'s "last marker wins" rule so a
        # template/sample reference earlier in the body loses to the
        # concluding line.
        msg = (
            f"I almost wrote {DOCS_NO_CHANGE_MARKER} but actually the README is "
            f"stale, so I'll commit a fix.\n\n{DOCS_NO_CHANGE_MARKER}"
        )
        verdict, _ = _parse_documentation_verdict(msg)
        self.assertEqual(verdict, DOCS_NO_CHANGE)

    def test_ambiguous_no_change_text_is_not_accepted(self) -> None:
        # Plain prose that sounds like a no-change result must NOT pass
        # without the explicit marker -- otherwise an agent that forgot
        # to commit a real docs update would silently close the stage.
        verdict, body = _parse_documentation_verdict(
            "Looks like no docs changes needed."
        )
        self.assertEqual(verdict, VERDICT_UNKNOWN)
        self.assertIn("no docs changes needed", body)

    def test_update_without_marker_is_unknown(self) -> None:
        # The 'updated' outcome is signalled by the new commit on the
        # branch, not by the parser. A message describing an update but
        # lacking the no-change marker must therefore stay 'unknown' so
        # the no-commit branch (parser-only) cannot silently accept it.
        verdict, _ = _parse_documentation_verdict(
            "Updated README.md with the new flag."
        )
        self.assertEqual(verdict, VERDICT_UNKNOWN)


class ParseDocumentationMarkerGuardTest(unittest.TestCase):
    """Reject inline, nonfinal, punctuated, or missing documentation markers."""

    def test_inline_marker_in_prose_is_unknown(self) -> None:
        # The marker must start its own line. An inline reference
        # embedded in a sentence -- e.g. "I cannot conclude DOCS:
        # NO_CHANGE because the README is stale" -- is exactly the kind
        # of ambiguous no-commit text the issue forbids accepting.
        verdict, _ = _parse_documentation_verdict(
            f"I cannot conclude {DOCS_NO_CHANGE_MARKER} because README is stale."
        )
        self.assertEqual(verdict, VERDICT_UNKNOWN)

    def test_nonfinal_marker_then_text_is_unknown(self) -> None:
        # The marker must be the FINAL non-whitespace content. A marker
        # line followed by an unresolved question must be rejected so an
        # agent's follow-up clarification can't silently close the stage.
        verdict, _ = _parse_documentation_verdict(
            f"{DOCS_NO_CHANGE_MARKER}\nBut I have a question about the API."
        )
        self.assertEqual(verdict, VERDICT_UNKNOWN)

    def test_trailing_punctuation_is_unknown(self) -> None:
        # `DOCS: NO_CHANGE.` (trailing punctuation) is rejected; the
        # contract is a machine-readable marker, not a sentence. Without
        # this, a markdown-trained agent's habit of ending sentences
        # with periods would silently mask the stricter rule.
        verdict, _ = _parse_documentation_verdict(
            f"All clear.\n\n{DOCS_NO_CHANGE_MARKER}."
        )
        self.assertEqual(verdict, VERDICT_UNKNOWN)

    def test_empty_message_returns_unknown(self) -> None:
        self.assertEqual(_parse_documentation_verdict(""), (VERDICT_UNKNOWN, ""))


class VerdictParserReexportTest(unittest.TestCase):
    """`workflow.py` re-exports the verdict parsers under their original
    names so historical `from orchestrator.workflow import _parse_*` call
    sites (and `patch.object(workflow, "_parse_*")` interception) keep
    working. The facade name must resolve to the same object the focused
    `workflow_messages` module defines."""

    def test_facade_reexports_focused_parsers(self) -> None:
        self.assertIs(
            workflow._parse_review_verdict,
            _parse_review_verdict,
        )
        self.assertIs(
            workflow._parse_documentation_verdict,
            _parse_documentation_verdict,
        )
