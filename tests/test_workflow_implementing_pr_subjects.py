# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Tests for implementing pr subjects behavior."""

from __future__ import annotations

import unittest

from tests import implementing_pr_test_support as support

workflow = support.workflow


class ConventionalSubjectHelperTest(unittest.TestCase):
    """Direct coverage for the regex helper, since the convention list grew
    beyond what the prompts spell out."""

    def test_accepts_basic_types(self) -> None:
        for subject in (
            "feat: add thing",
            "fix: bug",
            "chore: bump dep",
            "docs: tweak",
            "refactor: rename foo",
            "test: cover edge case",
            "perf: speed it up",
            "ci: fix workflow",
        ):
            self.assertTrue(
                workflow._is_conventional_subject(subject),
                f"expected conventional: {subject!r}",
            )

    def test_accepts_scope_and_breaking(self) -> None:
        self.assertTrue(workflow._is_conventional_subject("feat(api): foo"))
        self.assertTrue(workflow._is_conventional_subject("fix!: bar"))
        self.assertTrue(workflow._is_conventional_subject("feat(api)!: baz"))

    def test_rejects_non_conventional(self) -> None:
        for subject in (
            "",
            "Add a thing",
            "wip: thing",
            "feat:",  # no subject after colon
            "feat:   ",  # whitespace-only subject
            "Feat: cap type",  # types must be lowercase
            "  feat: leading",  # leading whitespace not accepted
        ):
            self.assertFalse(
                workflow._is_conventional_subject(subject),
                f"expected non-conventional: {subject!r}",
            )


class PrefixedSubjectHelperTest(unittest.TestCase):
    """`_is_prefixed_subject` is broader than `_is_conventional_subject`: it
    accepts any lowercase `<token>: <subject>` prefix, so repo-local styles
    survive, while still rejecting prose and bare prefixes."""

    def test_accepts_conventional_and_local_prefixes(self) -> None:
        for subject in (
            "feat: add thing",
            "fix(api)!: drop endpoint",
            "event: add the gala",  # not a Conventional type
            "career: open a role",
            "ui: tweak the spacing",
        ):
            self.assertTrue(
                workflow._is_prefixed_subject(subject),
                f"expected prefixed: {subject!r}",
            )

    def test_rejects_prose_and_bare_prefixes(self) -> None:
        for subject in (
            "",
            "updated stuff",  # no colon
            "fixed it",  # no colon
            "Add a thing",  # not prefixed
            "Note: capitalized token",  # token must start lowercase
            "event:",  # no subject after colon
            "event:   ",  # whitespace-only subject
            "  event: leading",  # leading whitespace not accepted
        ):
            self.assertFalse(
                workflow._is_prefixed_subject(subject),
                f"expected non-prefixed: {subject!r}",
            )
