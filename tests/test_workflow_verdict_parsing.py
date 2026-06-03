# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Verdict parsers used by review and documentation stages: marker shape,
case insensitivity, last-marker-wins semantics, and the strict rules that
keep ambiguous prose from being misread as a structured outcome."""
from __future__ import annotations

import os
import unittest

os.environ.setdefault("ORCHESTRATOR_SKIP_DOTENV", "1")

from orchestrator.workflow import _parse_documentation_verdict, _parse_review_verdict


class ParseReviewVerdictTest(unittest.TestCase):
    def test_approved_alone_on_line(self) -> None:
        self.assertEqual(
            _parse_review_verdict("Looks good.\n\nVERDICT: APPROVED"),
            ("approved", "Looks good."),
        )

    def test_changes_requested_with_numbered_list(self) -> None:
        msg = "1. Fix typo in README\n2. Add a test for the empty case\n\nVERDICT: CHANGES_REQUESTED"
        verdict, body = _parse_review_verdict(msg)
        self.assertEqual(verdict, "changes_requested")
        self.assertIn("1. Fix typo in README", body)
        self.assertNotIn("VERDICT", body)

    def test_inline_marker_is_accepted(self) -> None:
        self.assertEqual(
            _parse_review_verdict("All good. VERDICT: APPROVED"),
            ("approved", "All good."),
        )

    def test_case_insensitive(self) -> None:
        verdict, _ = _parse_review_verdict("verdict: approved")
        self.assertEqual(verdict, "approved")

    def test_last_marker_wins(self) -> None:
        msg = "I considered VERDICT: APPROVED but a test fails.\nVERDICT: CHANGES_REQUESTED"
        verdict, _ = _parse_review_verdict(msg)
        self.assertEqual(verdict, "changes_requested")

    def test_no_marker_returns_unknown(self) -> None:
        self.assertEqual(
            _parse_review_verdict("looks fine to me"),
            ("unknown", "looks fine to me"),
        )

    def test_empty_message_returns_unknown(self) -> None:
        self.assertEqual(_parse_review_verdict(""), ("unknown", ""))


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
                "Diff is internal-only; nothing user-visible changed.\n\nDOCS: NO_CHANGE"
            ),
            ("no_change", "Diff is internal-only; nothing user-visible changed."),
        )

    def test_no_change_marker_case_insensitive(self) -> None:
        verdict, _ = _parse_documentation_verdict("docs: no_change")
        self.assertEqual(verdict, "no_change")

    def test_last_marker_wins(self) -> None:
        # Mirrors `_parse_review_verdict`'s "last marker wins" rule so a
        # template/sample reference earlier in the body loses to the
        # concluding line.
        msg = (
            "I almost wrote DOCS: NO_CHANGE but actually the README is "
            "stale, so I'll commit a fix.\n\nDOCS: NO_CHANGE"
        )
        verdict, _ = _parse_documentation_verdict(msg)
        self.assertEqual(verdict, "no_change")

    def test_ambiguous_no_change_text_is_not_accepted(self) -> None:
        # Plain prose that sounds like a no-change result must NOT pass
        # without the explicit marker -- otherwise an agent that forgot
        # to commit a real docs update would silently close the stage.
        verdict, body = _parse_documentation_verdict(
            "Looks like no docs changes needed."
        )
        self.assertEqual(verdict, "unknown")
        self.assertIn("no docs changes needed", body)

    def test_update_description_without_marker_is_unknown(self) -> None:
        # The 'updated' outcome is signalled by the new commit on the
        # branch, not by the parser. A message describing an update but
        # lacking the no-change marker must therefore stay 'unknown' so
        # the no-commit branch (parser-only) cannot silently accept it.
        verdict, _ = _parse_documentation_verdict(
            "Updated README.md with the new flag."
        )
        self.assertEqual(verdict, "unknown")

    def test_inline_marker_in_prose_is_unknown(self) -> None:
        # The marker must start its own line. An inline reference
        # embedded in a sentence -- e.g. "I cannot conclude DOCS:
        # NO_CHANGE because the README is stale" -- is exactly the kind
        # of ambiguous no-commit text the issue forbids accepting.
        verdict, _ = _parse_documentation_verdict(
            "I cannot conclude DOCS: NO_CHANGE because README is stale."
        )
        self.assertEqual(verdict, "unknown")

    def test_non_final_marker_followed_by_text_is_unknown(self) -> None:
        # The marker must be the FINAL non-whitespace content. A marker
        # line followed by an unresolved question must be rejected so an
        # agent's follow-up clarification can't silently close the stage.
        verdict, _ = _parse_documentation_verdict(
            "DOCS: NO_CHANGE\nBut I have a question about the API."
        )
        self.assertEqual(verdict, "unknown")

    def test_marker_with_trailing_punctuation_is_unknown(self) -> None:
        # `DOCS: NO_CHANGE.` (trailing punctuation) is rejected; the
        # contract is a machine-readable marker, not a sentence. Without
        # this, a markdown-trained agent's habit of ending sentences
        # with periods would silently mask the stricter rule.
        verdict, _ = _parse_documentation_verdict("All clear.\n\nDOCS: NO_CHANGE.")
        self.assertEqual(verdict, "unknown")

    def test_empty_message_returns_unknown(self) -> None:
        self.assertEqual(_parse_documentation_verdict(""), ("unknown", ""))
