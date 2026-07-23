# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

from tests.base_sync_scenarios import PUSH_PATCH, REBASE_PATCH

from tests import base_sync_test_support as support


def _assert_clean_publication(test_case, fixture, scenario) -> None:
    scenario[REBASE_PATCH].assert_called_once()
    push = scenario[PUSH_PATCH]
    push.assert_called_once()
    test_case.assertEqual(
        push.call_args.kwargs.get(support.FORCE_WITH_LEASE_KWARG),
        support.BEFORE_SHA,
    )
    test_case.assertIn(
        (support.ISSUE, support.LABEL_VALIDATING),
        fixture.gh.label_history,
    )
    test_case.assertNotIn(
        (support.ISSUE, support.LABEL_RESOLVING_CONFLICT),
        fixture.gh.label_history,
    )


def _assert_clean_state_comments(test_case, fixture) -> None:
    test_case.assertEqual(len(fixture.gh.posted_pr_comments), 1)
    test_case.assertEqual(
        fixture.gh.posted_pr_comments[0][0],
        support.PR_NUMBER,
    )
    test_case.assertIn(
        support.LABEL_VALIDATING,
        fixture.gh.posted_pr_comments[0][1],
    )
    state = fixture.gh.pinned_data(support.ISSUE)
    test_case.assertEqual(state.get(support.KEY_REVIEW_ROUND), 0)
    test_case.assertIsNone(state.get(support.KEY_CONFLICT_ROUND))


def _assert_clean_events(test_case, fixture) -> None:
    rebased = [
        event for event in fixture.gh.recorded_events if event.get(support.EVENT_FIELD) == support.EVENT_BASE_REBASED
    ]
    test_case.assertEqual(len(rebased), 1)
    test_case.assertEqual(rebased[0].get(support.SHA_FIELD), support.AFTER_SHA)
    test_case.assertEqual(rebased[0].get("stage"), support.LABEL_IN_REVIEW)
    conflict_rounds = [
        event for event in fixture.gh.recorded_events if event.get(support.EVENT_FIELD) == support.EVENT_CONFLICT_ROUND
    ]
    test_case.assertEqual(conflict_rounds, [])


def _assert_conflict_publication(test_case, fixture, scenario) -> None:
    scenario[REBASE_PATCH].assert_called_once()
    abort_calls = [
        recorded_call
        for recorded_call in scenario["hardened"].call_args_list
        if recorded_call.args[:2] == (support.REBASE_COMMAND, support.ABORT_FLAG)
    ]
    test_case.assertEqual(len(abort_calls), 1)
    scenario[PUSH_PATCH].assert_not_called()
    test_case.assertIn(
        (support.ISSUE, support.LABEL_RESOLVING_CONFLICT),
        fixture.gh.label_history,
    )
    test_case.assertNotIn(
        (support.ISSUE, support.LABEL_VALIDATING),
        fixture.gh.label_history,
    )


def _assert_conflict_state_event(test_case, fixture) -> None:
    test_case.assertEqual(len(fixture.gh.posted_pr_comments), 1)
    test_case.assertIn(
        "conflicted file(s)",
        fixture.gh.posted_pr_comments[0][1],
    )
    state = fixture.gh.pinned_data(support.ISSUE)
    test_case.assertEqual(state.get(support.KEY_CONFLICT_ROUND), 0)
    entered = []
    for event in fixture.gh.recorded_events:
        if event.get(support.EVENT_FIELD) != support.EVENT_CONFLICT_ROUND:
            continue
        if event.get("action") == "entered":
            entered.append(event)
    test_case.assertEqual(len(entered), 1)
    test_case.assertEqual(entered[0].get("stage"), support.LABEL_IN_REVIEW)
    test_case.assertEqual(
        entered[0].get(support.SHA_FIELD),
        support.CONFLICT_PR_HEAD_SHA,
    )


def _assert_push_failure_git(test_case, fixture, scenario) -> None:
    reset_calls = [
        recorded_call
        for recorded_call in scenario["hardened"].call_args_list
        if recorded_call.args[:3] == (support.RESET_COMMAND, support.HARD_RESET_FLAG, support.BEFORE_SHA)
    ]
    test_case.assertEqual(len(reset_calls), 1)
    test_case.assertEqual(fixture.gh.posted_pr_comments, [])


def _assert_push_failure_state(test_case, fixture) -> None:
    test_case.assertEqual(fixture.gh.label_history, [])
    state = fixture.gh.pinned_data(support.ISSUE)
    test_case.assertIsNone(state.get(support.KEY_REVIEW_ROUND))
    test_case.assertTrue(state.get(support.KEY_AWAITING_HUMAN))
    test_case.assertEqual(
        state.get(support.KEY_PARK_REASON),
        support.PARK_PUSH_FAILED,
    )
    test_case.assertEqual(len(fixture.gh.posted_comments), 1)
    test_case.assertIn("force-with-lease", fixture.gh.posted_comments[0][1])
