# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Dashboard issue-filter parsing tests."""

import unittest


from tests.dashboard_reload_helpers import (
    reload_dashboard as _reload,
)


ISSUE_NUMBER = 42


class ParseIssueNumberTest(unittest.TestCase):
    def test_bare_int(self) -> None:
        _, dashboard = _reload()
        self.assertEqual(dashboard.parse_issue_number("42"), ISSUE_NUMBER)

    def test_hash_prefix_and_whitespace(self) -> None:
        _, dashboard = _reload()
        self.assertEqual(dashboard.parse_issue_number(" #42 "), ISSUE_NUMBER)
        self.assertEqual(dashboard.parse_issue_number("# 42"), ISSUE_NUMBER)

    def test_empty_returns_none(self) -> None:
        _, dashboard = _reload()
        self.assertIsNone(dashboard.parse_issue_number(""))
        self.assertIsNone(dashboard.parse_issue_number("   "))
        self.assertIsNone(dashboard.parse_issue_number("#"))

    def test_non_numeric_returns_none(self) -> None:
        _, dashboard = _reload()
        self.assertIsNone(dashboard.parse_issue_number("abc"))
        self.assertIsNone(dashboard.parse_issue_number("#abc"))

    def test_non_positive_returns_none(self) -> None:
        # GitHub issue numbers start at 1; 0 and negatives are not
        # valid drill-down targets.
        _, dashboard = _reload()
        self.assertIsNone(dashboard.parse_issue_number("0"))
        self.assertIsNone(dashboard.parse_issue_number("-3"))
