# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

from tests import base_sync_test_support as support


def _assert_hardened_calls(test_case, scenario, *prefixes: tuple[str, ...]) -> None:
    for prefix in prefixes:
        matching = [
            recorded_call
            for recorded_call in scenario["hardened"].call_args_list
            if recorded_call.args[: len(prefix)] == prefix
        ]
        test_case.assertEqual(
            len(matching),
            1,
            scenario["hardened"].call_args_list,
        )


def _assert_not_called(test_case, scenario, *aliases: str) -> None:
    for alias in aliases:
        test_case.assertEqual(scenario[alias].call_count, 0)


def _assert_parked_without_anchor(
    test_case,
    fixture,
    reason: str,
    *,
    message_fragment: str | None = None,
) -> None:
    test_case.assertEqual(fixture.gh.label_history, [])
    state = fixture.gh.pinned_data(support.ISSUE)
    test_case.assertTrue(state.get(support.KEY_AWAITING_HUMAN))
    test_case.assertEqual(state.get(support.KEY_PARK_REASON), reason)
    test_case.assertIsNone(state.get(support.KEY_PENDING_PUSH_SHA))
    if message_fragment is not None:
        test_case.assertEqual(len(fixture.gh.posted_comments), 1)
        test_case.assertIn(
            message_fragment,
            fixture.gh.posted_comments[0][1],
        )
