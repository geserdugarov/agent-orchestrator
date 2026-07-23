# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Stage-specific terminal-drain metadata tests."""
from __future__ import annotations

import unittest

from tests import workflow_drain_test_support as support


class DrainReviewPrMetadataTest(unittest.TestCase, support._DrainTestMixin):
    """Terminal events preserve stage-specific round metadata."""

    def test_conflict_route_keeps_zero_round(
        self,
    ) -> None:
        # Legacy / manually-relabelled `resolving_conflict` states may
        # land in the terminal arcs without `conflict_round` ever being
        # seeded (the in_review route normally initializes it to 0
        # before flipping the label). The pre-refactor inline code
        # coerced the value via `int(state.get("conflict_round") or 0)`
        # so the audit record always carried the field. `build_event_record`
        # drops None-valued extras, so the helper must keep that coercion
        # for `stage="resolving_conflict"` -- otherwise legacy states
        # silently lose `conflict_round` from `pr_merged` /
        # `pr_closed_without_merge` events.
        merged_scenario = support._DrainScenario(
            support._CONFLICT_MERGED_ISSUE_NUMBER,
            support._CONFLICT_MERGED_PR_NUMBER,
            True,
            support.STATE_CLOSED,
            support.LABEL_RESOLVING_CONFLICT,
            sha="feed1234",
        )
        # Deliberately omit `conflict_round` from the pinned state.
        merged_event = self._terminal_event(
            merged_scenario,
            support.EVENT_PR_MERGED,
        )
        self.assertEqual(merged_event[support._STAGE_KEY], support.LABEL_RESOLVING_CONFLICT)
        # Field must be present (build_event_record drops None), and
        # the coerced default must be 0.
        self.assertIn(support._CONFLICT_ROUND_KEY, merged_event)
        self.assertEqual(merged_event[support._CONFLICT_ROUND_KEY], 0)

        # Same coercion for the closed-without-merge arc.
        closed_scenario = support._DrainScenario(
            support._CONFLICT_CLOSED_ISSUE_NUMBER,
            support._CONFLICT_CLOSED_PR_NUMBER,
            False,
            support.STATE_CLOSED,
            support.LABEL_RESOLVING_CONFLICT,
            sha="feed5678",
        )
        closed_event = self._terminal_event(
            closed_scenario,
            support.EVENT_PR_CLOSED_WITHOUT_MERGE,
        )
        self.assertIn(support._CONFLICT_ROUND_KEY, closed_event)
        self.assertEqual(closed_event[support._CONFLICT_ROUND_KEY], 0)

    def test_review_terminal_omits_missing_round(self) -> None:
        # The other two stages have always passed the raw
        # `state.get("conflict_round")` through, so a missing counter
        # naturally drops out via `build_event_record`. Pin that contract
        # so a future refactor doesn't accidentally start coercing for
        # `in_review` / `fixing` and start emitting a `conflict_round=0`
        # field on states that never had the counter.
        scenario = support._DrainScenario(
            support._REVIEW_MERGED_ISSUE_NUMBER,
            support._REVIEW_MERGED_PR_NUMBER,
            True,
            support.STATE_CLOSED,
            support.LABEL_IN_REVIEW,
            sha="cafe5678",
        )
        merged_event = self._terminal_event(scenario, support.EVENT_PR_MERGED)
        self.assertNotIn(support._CONFLICT_ROUND_KEY, merged_event)
