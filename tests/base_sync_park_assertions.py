# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

from tests import base_sync_test_support as support
from tests.base_sync_scenarios import PUSH_PATCH, REBASE_PATCH


def _assert_park_state(
    test_case,
    fixture,
    *,
    reason: str,
    watermark: int,
) -> None:
    state = fixture.gh.pinned_data(support.ISSUE)
    test_case.assertTrue(state.get(support.KEY_AWAITING_HUMAN))
    test_case.assertEqual(state.get(support.KEY_PARK_REASON), reason)
    test_case.assertEqual(
        state.get(support.KEY_LAST_ACTION_COMMENT_ID),
        watermark,
    )


def _assert_retry_success(
    test_case,
    fixture,
    scenario,
    *,
    watermark: int,
) -> None:
    state = fixture.gh.pinned_data(support.ISSUE)
    test_case.assertFalse(state.get(support.KEY_AWAITING_HUMAN))
    test_case.assertIsNone(state.get(support.KEY_PARK_REASON))
    test_case.assertEqual(
        state.get(support.KEY_LAST_ACTION_COMMENT_ID),
        watermark,
    )
    scenario[REBASE_PATCH].assert_called_once()
    scenario[PUSH_PATCH].assert_called_once()
    test_case.assertIn(
        (support.ISSUE, support.LABEL_VALIDATING),
        fixture.gh.label_history,
    )


def _assert_scenario_idle(test_case, fixture, scenario) -> None:
    scenario[REBASE_PATCH].assert_not_called()
    scenario[PUSH_PATCH].assert_not_called()
    test_case.assertEqual(fixture.gh.label_history, [])
