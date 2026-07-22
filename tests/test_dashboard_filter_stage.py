# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Dashboard stage-filter resolution tests."""

import unittest


from tests.dashboard_reload_helpers import (
    reload_dashboard as _reload,
)


STAGE_IMPLEMENTING = "implementing"


STAGE_VALIDATING = "validating"


class ResolveStageFilterTest(unittest.TestCase):
    """The stage multiselect default ('all known non-null stages')
    must collapse to `stages=None` so the read-model query does
    not emit a `stage IN (...)` clause that silently excludes
    NULL-stage rows. NULL stages are a legitimate case --
    `stage_evaluation` writes `stage=None` when the issue
    carries no workflow label. The cleared-multiselect signal
    (`[]`) must stay distinct so the reviewer-documented "show
    nothing" path still works.
    """

    def test_all_selected_collapses_to_none(self) -> None:
        _, dashboard = _reload()
        resolved = dashboard.resolve_stage_filter(
            selected=[STAGE_IMPLEMENTING, STAGE_VALIDATING],
            available=(STAGE_IMPLEMENTING, STAGE_VALIDATING),
        )
        self.assertIsNone(resolved)

    def test_no_available_options_returns_none(self) -> None:
        # Empty filter options (DB is empty or has no non-null
        # stages yet) collapses to `None` so the read-model query
        # runs unconstrained on the stage column.
        _, dashboard = _reload()
        resolved = dashboard.resolve_stage_filter(selected=[], available=())
        self.assertIsNone(resolved)

    def test_cleared_multiselect_returns_empty_list(self) -> None:
        # Options exist but the operator cleared the selection.
        # The read model encodes `[]` as a tautologically-false
        # predicate; without this branch the cleared state would
        # be indistinguishable from the all-selected default.
        _, dashboard = _reload()
        resolved = dashboard.resolve_stage_filter(
            selected=[],
            available=(STAGE_IMPLEMENTING, STAGE_VALIDATING),
        )
        self.assertEqual(resolved, [])

    def test_proper_subset_passes_through(self) -> None:
        _, dashboard = _reload()
        resolved = dashboard.resolve_stage_filter(
            selected=[STAGE_IMPLEMENTING],
            available=(STAGE_IMPLEMENTING, STAGE_VALIDATING),
        )
        self.assertEqual(resolved, [STAGE_IMPLEMENTING])
