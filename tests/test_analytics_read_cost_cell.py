# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import unittest
from decimal import Decimal

from orchestrator.analytics.read_dashboard import _cost_cell as _dashboard_cost_cell
from orchestrator.analytics.read_rollup import _cost_cell as _rollup_cost_cell

# The float zero every empty / null / zero cell collapses to, and a
# populated Decimal cost with its float conversion.
_ZERO_COST = 0.0
_POPULATED_COST = Decimal("12.50")
_POPULATED_FLOAT = 12.5


class CostCellTest(unittest.TestCase):
    """`_cost_cell` reads a nullable USD cost column as a float: a missing
    column, a SQL null, or a zero all collapse to 0.0, and any populated
    value converts to its float -- the mapping the `_*_from_row` builders
    rely on when materializing cost fields from a DB row."""

    def test_missing_null_zero_collapse_to_float(self) -> None:
        for cost_cell in (_dashboard_cost_cell, _rollup_cost_cell):
            # An index past the row end, a SQL null, and a recorded
            # zero all collapse to the same float zero.
            self.assertEqual(cost_cell(("only-one",), 5), _ZERO_COST)
            self.assertEqual(cost_cell((None,), 0), _ZERO_COST)
            self.assertEqual(cost_cell((Decimal("0"),), 0), _ZERO_COST)
            self.assertIsInstance(cost_cell((None,), 0), float)

    def test_populated_value_converts_to_float(self) -> None:
        for cost_cell in (_dashboard_cost_cell, _rollup_cost_cell):
            self.assertEqual(cost_cell((_POPULATED_COST,), 0), _POPULATED_FLOAT)
            self.assertIsInstance(cost_cell((_POPULATED_COST,), 0), float)


if __name__ == "__main__":
    unittest.main()
