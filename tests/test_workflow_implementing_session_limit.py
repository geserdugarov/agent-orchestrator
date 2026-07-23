# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Tests for implementing session limit behavior."""

from __future__ import annotations

import unittest

from tests import implementing_retry_test_support as support

DEFAULT_SESSION = support.DEFAULT_SESSION
_agent = support._agent
workflow = support.workflow


class SessionLimitMessageClassifierTest(unittest.TestCase):
    """A session/usage-quota notice returned as the CLI's FINAL message is a
    retryable session-failure, not a real agent question. `_on_question` keys
    the retryable `agent_silent` park off `_is_session_limit_message`, so the
    classifier must accept the known phrasings (including a curly apostrophe)
    as a prefix while ignoring a plain question or a mid-answer mention.
    """

    def test_matches_known_session_limit_phrasings(self) -> None:
        for last_message in (
            # The #705 shape, verbatim.
            "You've hit your session limit · resets 7pm (Asia/Novosibirsk)",
            # Curly apostrophe still hits (normalized before matching).
            "You’ve hit your session limit · resets 7pm",
            "You've reached your usage limit for now",
            "Claude AI usage limit reached|1712345678",
            # Mixed casing / leading whitespace still trip the prefix match.
            "  CLAUDE USAGE LIMIT REACHED",
        ):
            with self.subTest(last_message=last_message):
                agent_result = _agent(session_id=DEFAULT_SESSION, last_message=last_message)
                self.assertTrue(
                    workflow._is_session_limit_message(agent_result),
                    f"{last_message!r} should classify as a session limit",
                )

    def test_ignores_question_and_midanswer_mention(self) -> None:
        for last_message in (
            "",
            "Should I prefer ruff or black for this?",
            # A dev discussing the concept mid-answer must not be caught --
            # the marker is matched as a prefix, not anywhere in the body.
            "I added a note about the session limit handling in fixing.py.",
        ):
            with self.subTest(last_message=last_message):
                agent_result = _agent(session_id=DEFAULT_SESSION, last_message=last_message)
                self.assertFalse(
                    workflow._is_session_limit_message(agent_result),
                    f"{last_message!r} must not classify as a session limit",
                )
