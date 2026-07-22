# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Dashboard reliability-tile projection tests."""

import unittest


from tests.dashboard_reload_helpers import (
    reload_dashboard as _reload,
)

_FULL_WINDOW_SUMMARY_RESOLVED = 12
_WINDOW_NEUTRAL_TONES_TOTAL_A = 20


FULL_WINDOW_AGENT_RUNS = 250


FULL_WINDOW_FAILURES = 4


FULL_WINDOW_TIMEOUTS = 17


KPI_AGENT_RUNS = "Agent runs"


KPI_FAILURES = "Failures"


KPI_TIMEOUTS = "Timeouts"


def _tile_value_tones(tiles) -> dict:
    """Project `reliability_tile_data` triples to `{label: (value, tone)}`."""
    return {label: (tile_value, tone) for tile_value, label, tone in tiles}


def _tile_values(tiles) -> dict:
    """Project `reliability_tile_data` triples to `{label: value}`."""
    return {label: tile_value for tile_value, label, _ in tiles}


def _tile_tones(tiles) -> dict:
    """Project `reliability_tile_data` triples to `{label: tone}`."""
    return {label: tone for _, label, tone in tiles}


class ReliabilityTileDataTest(unittest.TestCase):
    """The redesigned reliability panel sources every tile from
    `Summary`'s window-wide aggregates so a long window with more
    than `DEFAULT_RECENT_AGENT_EXITS` (100) rows still sees every
    timeout / failure -- the earlier draft computed these off the
    LIMIT-capped recent-runs read and silently undercounted."""

    def test_timeouts_use_full_window_summary(self) -> None:
        _, dashboard = _reload()
        # Window holds far more agent runs than the recent-runs cap, with
        # failures and timeouts mixed in.
        summary = self._summary(
            total_agent_runs=FULL_WINDOW_AGENT_RUNS,
            failed_agent_runs=FULL_WINDOW_FAILURES,
            timed_out_agent_runs=FULL_WINDOW_TIMEOUTS,
        )
        tiles = dashboard.reliability_tile_data(
            summary,
            resolved=_FULL_WINDOW_SUMMARY_RESOLVED,
            rejected=2,
        )
        by_label = _tile_value_tones(tiles)
        # Headline tiles all pulled off Summary directly:
        self.assertEqual(by_label[KPI_AGENT_RUNS][0], FULL_WINDOW_AGENT_RUNS)
        self.assertEqual(by_label[KPI_FAILURES][0], FULL_WINDOW_FAILURES)
        self.assertEqual(by_label[KPI_TIMEOUTS][0], FULL_WINDOW_TIMEOUTS)
        # Tone flips when the count crosses zero so the CSS class
        # paints the tile.
        self.assertEqual(by_label[KPI_TIMEOUTS][1], "bad")
        self.assertEqual(by_label[KPI_FAILURES][1], "warn")

    def test_zero_runs_does_not_divide_by_zero(self) -> None:
        # Empty window: success rate collapses to 0% (no runs, no
        # successes) instead of raising a ZeroDivisionError. The
        # redesigned page renders the tile anyway so the operator
        # can confirm the window really is empty.
        _, dashboard = _reload()
        summary = self._summary(
            total_agent_runs=0,
            failed_agent_runs=0,
            timed_out_agent_runs=0,
        )
        tiles = dashboard.reliability_tile_data(summary)
        by_label = _tile_values(tiles)
        self.assertEqual(by_label[KPI_AGENT_RUNS], 0)
        self.assertEqual(by_label["Success rate"], "0%")
        self.assertEqual(by_label[KPI_TIMEOUTS], 0)

    def test_clean_window_has_neutral_tones(self) -> None:
        # No failures, no timeouts: the warn / bad tones drop off
        # so the panel reads as healthy at a glance.
        _, dashboard = _reload()
        summary = self._summary(
            total_agent_runs=_WINDOW_NEUTRAL_TONES_TOTAL_A,
            failed_agent_runs=0,
            timed_out_agent_runs=0,
        )
        tiles = dashboard.reliability_tile_data(summary)
        by_label = _tile_tones(tiles)
        self.assertEqual(by_label[KPI_FAILURES], "")
        self.assertEqual(by_label[KPI_TIMEOUTS], "")

    def _summary(self, **kw):
        _, dashboard = _reload()
        from orchestrator.analytics.read import Summary

        return Summary(**kw)
