# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Focused assertions shared by provider pricing tests."""

import unittest


def assert_cost(
    test_case: unittest.TestCase,
    metrics,
    expected_cost: float,
    *,
    places: int,
) -> None:
    test_case.assertIsNotNone(metrics.cost_usd)
    test_case.assertAlmostEqual(metrics.cost_usd, expected_cost, places=places)
