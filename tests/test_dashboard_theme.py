# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Tests for the plotly-free theme tokens in `orchestrator.dashboard_theme`.

These tests do NOT require plotly: the theme module is intentionally
dependency-free so the dashboard chrome can pull semantic colors
without forcing the optional `dashboard` group on every caller. The
chart-builder tests live in `tests/test_dashboard_charts.py` and skip
cleanly when plotly is absent.
"""
from __future__ import annotations

import unittest

from orchestrator import dashboard_theme as theme


class ColorForTest(unittest.TestCase):

    def test_explicit_palette_wins(self) -> None:
        # An explicit mapping always overrides domain-position lookup,
        # so the stage colors stay stable even if the chart re-orders
        # rows.
        self.assertEqual(
            theme.color_for(
                "implementing",
                ["implementing", "validating"],
                explicit=theme.STAGE_COLORS,
            ),
            theme.STAGE_COLORS["implementing"],
        )

    def test_domain_position_drives_color(self) -> None:
        # Without an explicit mapping, the n-th entry of the domain
        # gets the n-th entry of `CATEGORICAL_PALETTE`. That property
        # is what makes "the same domain in the same order" produce
        # the same colors across chart re-renders.
        self.assertEqual(
            theme.color_for("a", ["a", "b", "c"]),
            theme.CATEGORICAL_PALETTE[0],
        )
        self.assertEqual(
            theme.color_for("b", ["a", "b", "c"]),
            theme.CATEGORICAL_PALETTE[1],
        )

    def test_unknown_key_in_domain_falls_through_to_hash(self) -> None:
        # `domain` is provided but the key is not in it -- the helper
        # should still return a palette color rather than raising.
        color = theme.color_for("zzz", ["a", "b"])
        self.assertIn(color, theme.CATEGORICAL_PALETTE)

    def test_no_domain_returns_palette_color(self) -> None:
        self.assertIn(
            theme.color_for("anything"), theme.CATEGORICAL_PALETTE
        )


class BaseLayoutTest(unittest.TestCase):

    def test_returns_plain_dict(self) -> None:
        # The layout must be JSON-friendly plain data; chart builders
        # splat it into `fig.update_layout(**...)`, so any non-dict
        # type would break the call site.
        layout = theme.base_layout()
        self.assertIsInstance(layout, dict)
        # The plot background matches the card surface (white) because
        # every chart in the redesigned page lives inside a card.
        self.assertEqual(layout["paper_bgcolor"], theme.CARD_BG)
        self.assertEqual(layout["plot_bgcolor"], theme.CARD_BG)
        self.assertIn("font", layout)
        self.assertNotIn("title", layout)

    def test_title_threads_through(self) -> None:
        layout = theme.base_layout(title="Hello")
        self.assertEqual(layout["title"]["text"], "Hello")
        # The top margin grows when a title is present so the title
        # has room to render above the plot area.
        self.assertGreaterEqual(layout["margin"]["t"], 24)


class PaletteContractTest(unittest.TestCase):
    """The categorical palettes are public contracts -- every value
    referenced by the chart builders must be present and resolve to a
    hex color string. Regressions here would silently break legend
    coloring on a live dashboard.
    """

    def test_event_colors_cover_known_events(self) -> None:
        for event in ("stage_enter", "stage_evaluation", "agent_exit"):
            self.assertIn(event, theme.EVENT_COLORS)
            self.assertTrue(theme.EVENT_COLORS[event].startswith("#"))

    def test_stage_colors_cover_workflow_labels(self) -> None:
        expected = {
            "decomposing", "blocked", "ready", "umbrella",
            "implementing", "validating", "documenting", "in_review",
            "fixing", "resolving_conflict", "question", "done",
            "rejected",
        }
        self.assertEqual(set(theme.STAGE_COLORS).intersection(expected),
                         expected)

    def test_cost_source_colors_cover_usage_tags(self) -> None:
        for tag in ("reported", "estimated", "unknown-price", "no-usage"):
            self.assertIn(tag, theme.COST_SOURCE_COLORS)

    def test_token_type_colors_cover_redesign(self) -> None:
        # The hero stacked-area chart uses these three bands; missing
        # any one of them would break the legend / fill on a live
        # dashboard.
        for label in ("Input", "Output", "Cache"):
            self.assertIn(label, theme.TOKEN_TYPE_COLORS)
            self.assertTrue(theme.TOKEN_TYPE_COLORS[label].startswith("#"))

    def test_backend_colors_cover_known_backends(self) -> None:
        for backend in ("claude", "codex", "unknown"):
            self.assertIn(backend, theme.BACKEND_COLORS)

    def test_review_round_colors_cover_buckets(self) -> None:
        for bucket in ("0", "1", "2", "3-5", "6+"):
            self.assertIn(bucket, theme.REVIEW_ROUND_COLORS)


class FormattersTest(unittest.TestCase):
    """The KPI strip and per-bar labels all run through these
    formatters. Pin the shape so a future tweak does not silently
    change what the operator reads on every card.
    """

    def test_fmt_money_handles_zero_and_small_values(self) -> None:
        self.assertEqual(theme.fmt_money(0), "$0.00")
        self.assertEqual(theme.fmt_money(4.5), "$4.50")
        self.assertEqual(theme.fmt_money(42), "$42")

    def test_fmt_money_uses_k_and_m_suffixes(self) -> None:
        self.assertEqual(theme.fmt_money(1_234), "$1.2K")
        self.assertEqual(theme.fmt_money(2_500_000), "$2.50M")

    def test_fmt_money_exact_uses_thousands(self) -> None:
        self.assertEqual(theme.fmt_money_exact(12_345.67), "$12,346")
        self.assertEqual(theme.fmt_money_exact(0), "$0")

    def test_fmt_tokens_compact(self) -> None:
        self.assertEqual(theme.fmt_tokens(0), "0")
        self.assertEqual(theme.fmt_tokens(999), "999")
        self.assertEqual(theme.fmt_tokens(1_500), "2K")
        self.assertEqual(theme.fmt_tokens(2_500_000), "2.5M")
        self.assertEqual(theme.fmt_tokens(12_000_000_000), "12B")

    def test_fmt_num_thousands(self) -> None:
        self.assertEqual(theme.fmt_num(1234567), "1,234,567")
        self.assertEqual(theme.fmt_num(0), "0")


class PageCssTest(unittest.TestCase):
    """`PAGE_CSS` is a single string injected verbatim through
    `st.markdown(..., unsafe_allow_html=True)` so the redesigned
    chrome renders inside Streamlit's container. Pin the shape so a
    future refactor does not silently drop the style tag.
    """

    def test_starts_with_style_tag(self) -> None:
        self.assertTrue(theme.PAGE_CSS.lstrip().startswith("<style>"))

    def test_carries_the_redesigned_palette(self) -> None:
        # Spot-check a few class names + colors the dashboard layout
        # depends on. A grep test is the cheapest gate against a
        # silent rename.
        for needle in (
            ".orch-topbar",
            ".orch-kpis",
            ".orch-card",
            ".orch-insight",
            theme.BACKGROUND,
            theme.ACCENT,
        ):
            self.assertIn(needle, theme.PAGE_CSS)

    def test_filterbar_styling_targets_anchor_via_has(self) -> None:
        # `.orch-filterbar` must actually paint the bordered
        # container the dashboard wraps the date controls in. The
        # earlier draft relied on `stMarkdown + stVerticalBlockBorderWrapper`
        # sibling adjacency, but every Streamlit element is wrapped
        # in its own `stElementContainer`, so that adjacency never
        # matched. The redesigned rule targets the wrapper directly
        # via `:has(.orch-filterbar-anchor)` and the dashboard renders
        # the anchor as the FIRST child inside the bordered container.
        self.assertIn(".orch-filterbar", theme.PAGE_CSS)
        self.assertIn(".orch-filterbar-anchor", theme.PAGE_CSS)
        self.assertIn(
            'div[data-testid="stVerticalBlockBorderWrapper"]:has(',
            theme.PAGE_CSS,
        )
        # The generic card rule intentionally avoids `:not(:has(...))`
        # because the embedded browser dropped that unsupported
        # compound selector and left ordinary cards unstyled.
        self.assertIn(
            'div[data-testid="stVerticalBlockBorderWrapper"] {',
            theme.PAGE_CSS,
        )
        self.assertIn("print-color-adjust: exact", theme.PAGE_CSS)
        self.assertNotIn(
            ":not(:has(.orch-filterbar-anchor))", theme.PAGE_CSS
        )

    def test_segmented_control_has_visible_selected_state(self) -> None:
        # The earlier draft just hid the radio dot, leaving the
        # active option indistinguishable from the inactive ones.
        # The redesigned rule wraps the radiogroup in a chip-colored
        # pill and lights up the selected label via `:has(input:checked)`.
        self.assertIn(
            'div[data-testid="stRadio"] > div[role="radiogroup"]',
            theme.PAGE_CSS,
        )
        self.assertIn(":has(input:checked)", theme.PAGE_CSS)

    def test_chrome_does_not_full_bleed_with_100vw(self) -> None:
        # `100vw` includes the vertical scrollbar's width but the
        # content area does not, so a full-bleed bar overflows by
        # ~15px on any page tall enough to scroll. Keep the topbar
        # and filter bar inside the content column instead.
        self.assertNotIn("width: 100vw", theme.PAGE_CSS)
        self.assertNotIn("calc(50% - 50vw)", theme.PAGE_CSS)


class StandaloneTokenMirrorTest(unittest.TestCase):
    """The reviewer pinned a visual mismatch between an earlier
    warm-cream rewrite and the standalone mock's :root block. These
    tests pin the cool-gray palette + geometry tokens against the
    mock's exact values so a future tweak cannot silently drift the
    palette back.
    """

    def test_page_chrome_matches_mock(self) -> None:
        # Cool-gray page, white cards, dark blue-gray ink and the
        # softer ink-2 / ink-3 muted text tokens.
        self.assertEqual(theme.BACKGROUND, "#f4f5f8")
        self.assertEqual(theme.CARD_BG, "#ffffff")
        self.assertEqual(theme.TEXT, "#1c2030")
        self.assertEqual(theme.MUTED_TEXT, "#565d72")
        self.assertEqual(theme.MUTED_TEXT_SOFT, "#8a90a3")
        self.assertEqual(theme.BORDER, "#e6e8ef")
        self.assertEqual(theme.GRID, "#eef0f5")
        self.assertEqual(theme.SURFACE, "#f0f1f6")

    def test_geometry_tokens_match_mock(self) -> None:
        # 14px radius / 20px card padding / 16px grid gap matches
        # the mock's `:root` block. An earlier draft used 10px / 14px
        # / 14px and the reviewer flagged the visual mismatch.
        self.assertEqual(theme.RADIUS, "14px")
        self.assertEqual(theme.CARD_PADDING, "20px")
        self.assertEqual(theme.GRID_GAP, "16px")
        self.assertEqual(theme.CONTENT_MAX_WIDTH, "1480px")

    def test_page_css_uses_radius_and_max_width_tokens(self) -> None:
        # `1480px` and `14px` need to surface in the CSS string, not
        # just the Python tokens, so the rendered layout matches.
        self.assertIn("1480px", theme.PAGE_CSS)
        self.assertIn("14px", theme.PAGE_CSS)
        # A sticky topbar + filterbar are what the reviewer asked for.
        self.assertIn("position: sticky", theme.PAGE_CSS)
        self.assertIn("top: 0", theme.PAGE_CSS)
        # The previous draft's 1240px content width must NOT
        # survive anywhere in the rendered CSS -- the mock stretches
        # to 1480px.
        self.assertNotIn("max-width: 1240px", theme.PAGE_CSS)
        # Cards use the `--orch-radius` token (14 px) -- not the old
        # hardcoded 10 px. The reliability tiles legitimately keep
        # their own smaller 10 px radius (mirroring the mock's
        # `.rel-tile`), so we anchor the regression check on the
        # block-container card rule rather than a global string
        # search.
        self.assertIn(
            "border-radius: var(--orch-radius) !important",
            theme.PAGE_CSS,
        )

    def test_ibm_plex_font_stack(self) -> None:
        # The mock specifies IBM Plex Sans / Mono. We list them at the
        # top of each font stack with the previous system fallbacks
        # so the dashboard still renders cleanly on browsers without
        # the bundled woff2 fonts.
        self.assertIn("IBM Plex Sans", theme.FONT_FAMILY)
        self.assertIn("IBM Plex Mono", theme.MONO_FONT_FAMILY)


if __name__ == "__main__":
    unittest.main()
