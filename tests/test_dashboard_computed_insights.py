# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Dashboard computed-insight tests."""

import unittest


from tests.dashboard_reload_helpers import (
    reload_dashboard as _reload,
)


_BANNERS_HEALTHY_WINDOW_AGENT = 50


_BANNERS_HEALTHY_WINDOW_COST = 10.0


_COVERAGE_EMITS_WARNING_RUNS = 70


_COVERAGE_EMITS_WARNI_SECONDARY = 20


_BELOW_THRESHOLD_SKIPS_RUNS = 99


COST_SOURCE_REPORTED = "reported"


COST_SOURCE_UNKNOWN_PRICE = "unknown-price"


class ComputeInsightsTest(unittest.TestCase):
    """The insight banners are derived computationally from the
    read-model rows; this test pins the threshold semantics so a
    future tuning pass changes them deliberately.
    """

    def test_no_banners_for_healthy_window(self) -> None:
        _, dashboard = _reload()
        summary = self._summary(
            events=100, agent_runs=_BANNERS_HEALTHY_WINDOW_AGENT, failed=0, cost=_BANNERS_HEALTHY_WINDOW_COST
        )
        self.assertEqual(dashboard.compute_insights(summary), [])

    def test_high_failure_rate_emits_error(self) -> None:
        _, dashboard = _reload()
        summary = self._summary(agent_runs=10, failed=3)
        banners = dashboard.compute_insights(summary)
        self.assertEqual(len(banners), 1)
        self.assertEqual(banners[0].severity, "error")
        self.assertIn("3 of 10", banners[0].message)

    def test_low_failure_rate_skips_banner(self) -> None:
        _, dashboard = _reload()
        summary = self._summary(agent_runs=100, failed=5)
        self.assertEqual(dashboard.compute_insights(summary), [])

    def test_unpriced_coverage_emits_warning(self) -> None:
        _, dashboard = _reload()
        from orchestrator.analytics.read import CostCoverageRow

        summary = self._summary()
        cov = [
            CostCoverageRow(cost_source=COST_SOURCE_REPORTED, runs=_COVERAGE_EMITS_WARNING_RUNS),
            CostCoverageRow(cost_source=COST_SOURCE_UNKNOWN_PRICE, runs=_COVERAGE_EMITS_WARNI_SECONDARY),
            CostCoverageRow(cost_source="unknown", runs=10),
        ]
        banners = dashboard.compute_insights(summary, cost_coverage_rows=cov)
        # 30 / 100 = 30% unpriced -- well over the 10% threshold.
        self.assertTrue(
            any(
                banner.severity == "warning" and "30 of 100" in banner.message
                for banner in banners
            )
        )

    def test_unpriced_below_threshold_skips(self) -> None:
        _, dashboard = _reload()
        from orchestrator.analytics.read import CostCoverageRow

        summary = self._summary()
        cov = [
            CostCoverageRow(cost_source=COST_SOURCE_REPORTED, runs=_BELOW_THRESHOLD_SKIPS_RUNS),
            CostCoverageRow(cost_source=COST_SOURCE_UNKNOWN_PRICE, runs=1),
        ]
        self.assertEqual(
            dashboard.compute_insights(summary, cost_coverage_rows=cov),
            [],
        )

    def _summary(
        self,
        *,
        events=0,
        cost=None,
        agent_runs=0,
        failed=0,
    ):
        _, dashboard = _reload()
        return dashboard.Summary(
            total_events=events,
            total_agent_runs=agent_runs,
            failed_agent_runs=failed,
            total_cost_usd=float() if cost is None else cost,
        )
