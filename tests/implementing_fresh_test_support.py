# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Focused assertions for implementing fresh-run and resume tests."""

from __future__ import annotations

from orchestrator import config
from tests import implementing_fixing_test_cases


def assert_pr_routing(test_case, scenario) -> None:
    github = scenario.github
    test_case.assertEqual(len(github.opened_prs), 1)
    opened_pr = github.opened_prs[0]
    test_case.assertTrue(
        implementing_fixing_test_cases.posted_comment_contains(
            github,
            f":sparkles: PR opened: #{opened_pr.number}",
        ),
    )
    test_case.assertIn((1, "validating"), github.label_history)
    test_case.assertNotIn((1, "in_review"), github.label_history)
    test_case.assertNotIn((1, "documenting"), github.label_history)


def assert_pr_state(test_case, scenario) -> None:
    opened_pr = scenario.github.opened_prs[0]
    state = scenario.github.pinned_data(1)
    test_case.assertEqual(state["pr_number"], opened_pr.number)
    test_case.assertEqual(
        state["branch"],
        "orchestrator/geserdugarov__agent-orchestrator/issue-1",
    )
    test_case.assertEqual(state["dev_agent"], config.DEV_AGENT)
    test_case.assertEqual(state["dev_session_id"], "sess-1")
    test_case.assertNotIn("codex_session_id", state)
    test_case.assertEqual(state["review_round"], 0)


def assert_human_reply_resume(
    test_case,
    github,
    mocks,
    run_agent_key,
    legacy_session,
) -> None:
    mocks[run_agent_key].assert_called_once()
    agent_call = mocks[run_agent_key].call_args
    test_case.assertEqual(agent_call.args[0], "codex")
    test_case.assertEqual(
        agent_call.kwargs.get("resume_session_id"),
        legacy_session,
    )
    followup = agent_call.args[1]
    test_case.assertIn("please use sqlite", followup)
    test_case.assertIn("NEVER start a background job", followup)
    test_case.assertEqual(len(github.opened_prs), 1)
    test_case.assertFalse(
        github.pinned_data(2).get("awaiting_human"),
    )


def assert_interrupted_resume_state(
    test_case,
    github,
    before_writes,
    issue_number,
    action_comment_id,
) -> None:
    test_case.assertEqual(github.write_state_calls, before_writes)
    state = github.pinned_data(issue_number)
    test_case.assertTrue(state.get("awaiting_human"))
    test_case.assertEqual(
        state.get("last_action_comment_id"),
        action_comment_id,
    )
    test_case.assertEqual(github.opened_prs, [])
    test_case.assertEqual(github.label_history, [])
    test_case.assertFalse(
        implementing_fixing_test_cases.posted_comment_contains(
            github,
            "agent needs your input",
        )
        or implementing_fixing_test_cases.posted_comment_contains(
            github,
            "timed out",
        ),
    )
