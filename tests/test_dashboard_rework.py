# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Dashboard rework-total tests."""

import unittest


from tests.dashboard_reload_helpers import (
    reload_dashboard as _reload,
)


_INITIAL_BUCKET_EXCLUDED_TOTA = 50.0


_INITIAL_BUCKET_EXCLU_SECONDARY = 20.0


_INITIAL_BUCKET_EXCLUD_TERTIARY = 70.0


_INITIAL_BUCKET_EXCLUDED_REWO = 20.0


_UNKNOWN_BUCKET_EXCLUDED_TOTA = 10.0


_UNKNOWN_BUCKET_EXCLU_SECONDARY = 5.0


_UNKNOWN_BUCKET_EXCLUD_TERTIARY = 15.0


_UNKNOWN_BUCKET_EXCLUDED_REWO = 5.0


BUCKET_INITIAL = "0"


BUCKET_FIRST_ROUND = "1"


class ReworkTotalsTest(unittest.TestCase):
    """The rework KPI tile reads off `rework_totals`. Pin the shape so
    a future tweak does not silently shift which buckets count as
    rework.
    """

    def test_initial_bucket_excluded(self) -> None:
        _, dashboard = _reload()
        from orchestrator.analytics.read import ReviewRoundBucketRow

        rows = [
            ReviewRoundBucketRow(bucket=BUCKET_INITIAL, runs=5, failed=0, total_cost_usd=_INITIAL_BUCKET_EXCLUDED_TOTA),
            ReviewRoundBucketRow(
                bucket=BUCKET_FIRST_ROUND, runs=2, failed=1, total_cost_usd=_INITIAL_BUCKET_EXCLU_SECONDARY
            ),
        ]
        total, rework = dashboard.rework_totals(rows)
        self.assertAlmostEqual(total, _INITIAL_BUCKET_EXCLUD_TERTIARY)
        self.assertAlmostEqual(rework, _INITIAL_BUCKET_EXCLUDED_REWO)

    def test_unknown_bucket_excluded(self) -> None:
        # `unknown` is pre-review work surfaced for visibility, NOT
        # rework -- exclude it from the rework cost.
        _, dashboard = _reload()
        from orchestrator.analytics.read import ReviewRoundBucketRow

        rows = [
            ReviewRoundBucketRow(bucket="unknown", runs=3, failed=0, total_cost_usd=_UNKNOWN_BUCKET_EXCLUDED_TOTA),
            ReviewRoundBucketRow(bucket="2", runs=1, failed=0, total_cost_usd=_UNKNOWN_BUCKET_EXCLU_SECONDARY),
        ]
        total, rework = dashboard.rework_totals(rows)
        self.assertAlmostEqual(total, _UNKNOWN_BUCKET_EXCLUD_TERTIARY)
        self.assertAlmostEqual(rework, _UNKNOWN_BUCKET_EXCLUDED_REWO)

    def test_empty_rows_returns_zero(self) -> None:
        _, dashboard = _reload()
        total, rework = dashboard.rework_totals([])
        self.assertAlmostEqual(total, float())
        self.assertAlmostEqual(rework, float())
