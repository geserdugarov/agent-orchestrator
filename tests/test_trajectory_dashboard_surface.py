# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Trajectory card-header and HTML responsibility-boundary tests."""

import unittest


def _td():
    from orchestrator import trajectory_dashboard as td
    return td


class CardHeaderHtmlTest(unittest.TestCase):

    def test_title_and_sub_escaped(self) -> None:
        html = _td()._card_header_html("Title <b>", "Sub & more")
        self.assertIn("orch-card-title", html)
        self.assertIn("Title &lt;b&gt;", html)
        self.assertIn("Sub &amp; more", html)


_LEAF_HTML_MEMBERS = (
    "_topbar_html",
    "_kpi_strip_html",
    "_card_header_html",
    "_meta_html",
    "_labeled_chips_html",
    "_run_usage_html",
    "_runs_table_html",
    "_run_picker_label",
    "_timeline_entry_html",
    "_timeline_with_usage",
    "_turn_usage_html",
)


class TrajectoryHtmlExtractionTest(unittest.TestCase):
    """The trajectory viewer's pure inline-HTML builders live in the
    Streamlit-free `orchestrator._trajectory_dashboard_html` leaf, and
    `orchestrator.trajectory_dashboard` exposes each under the same name
    so the page (and these tests) resolve to the same object.
    """

    def test_html_members_defined_in_leaf(self) -> None:
        from orchestrator import _trajectory_dashboard_html as leaf
        for name in _LEAF_HTML_MEMBERS:
            with self.subTest(name=name):
                self.assertEqual(
                    getattr(leaf, name).__module__,
                    "orchestrator._trajectory_dashboard_html",
                )

    def test_page_reaches_the_leaf_objects(self) -> None:
        from orchestrator import _trajectory_dashboard_html as leaf
        page = _td()
        for name in _LEAF_HTML_MEMBERS:
            with self.subTest(name=name):
                self.assertIs(getattr(page, name), getattr(leaf, name))
