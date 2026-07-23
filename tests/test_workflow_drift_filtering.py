# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Workflow drift comment-filter and marker tests."""
from __future__ import annotations

import unittest

from orchestrator import workflow

from tests import workflow_drift_test_support as support


class OrchCommentMarkerSurvivesIdCapTest(unittest.TestCase):
    """Reviewer point 3: `orchestrator_comment_ids` is capped, but the
    hash scans every comment. Once an old orchestrator-comment id is
    evicted from the cap, an id-only filter would start including the
    bot comment in the hash and trigger false drift each tick. The body
    marker (`_ORCH_COMMENT_MARKER`) must keep the hash stable."""

    def test_unknown_id_bot_comment_is_excluded(
        self,
    ) -> None:
        # Simulate an orchestrator comment whose id has been evicted
        # from the bounded cap. Its body still carries the marker
        # (because every orchestrator comment is posted with it), so
        # the hash filter must drop it.
        bot_body = f"picking this up\n\n{workflow._ORCH_COMMENT_MARKER}"
        bot = support.FakeComment(
            id=support._EVICTED_BOT_COMMENT_ID,
            body=bot_body,
            user=support.FakeUser(support.TRUSTED_AUTHOR),
        )
        human = support.FakeComment(
            id=support._HUMAN_COMMENT_ID,
            body="please reconsider",
            user=support.FakeUser(support.TRUSTED_AUTHOR),
        )
        issue_with_just_human = support.make_issue(1, comments=[human])
        issue_with_both = support.make_issue(1, comments=[bot, human])
        # `orchestrator_ids` is EMPTY (the id was evicted from the cap),
        # but the hash must still match because the marker identifies
        # the bot comment.
        self.assertEqual(
            workflow._compute_user_content_hash(
                issue_with_just_human, set()
            ),
            workflow._compute_user_content_hash(
                issue_with_both, set()
            ),
        )

    def test_marker_is_appended_by_post_helpers(self) -> None:
        # Every orchestrator-posted comment must carry the marker so
        # the hash filter survives id-cap eviction.
        gh = support.FakeGitHubClient()
        issue = support.make_issue(1)
        gh.add_issue(issue)
        state = workflow.PinnedState()
        workflow._post_issue_comment(gh, issue, state, "hello")
        # The body actually written to the issue carries the marker.
        last_body = issue.comments[-1].body
        self.assertIn(workflow._ORCH_COMMENT_MARKER, last_body)
        # And it starts with the original body text.
        self.assertTrue(last_body.startswith("hello"))

    def test_marker_is_idempotent_on_double_wrap(self) -> None:
        # Defensive: a caller that already passes a body containing the
        # marker (e.g. a future helper forwards a pre-built body) must
        # not get the marker appended twice -- two markers in one body
        # is harmless but ugly, and an idempotent wrap also keeps
        # `_with_orch_marker` safe to call from helper chains.
        marked = workflow._with_orch_marker("hi")
        twice = workflow._with_orch_marker(marked)
        self.assertEqual(marked, twice)
        self.assertEqual(twice.count(workflow._ORCH_COMMENT_MARKER), 1)


class HashFiltersBotUsersTest(unittest.TestCase):
    """Reviewer point 2: third-party Bot/App accounts (Dependabot,
    Renovate, CI bots) post comments structurally on long-lived issues.
    The hash must filter them by GitHub's `user.type == "Bot"` flag so
    a periodic bot comment doesn't re-trigger drift on every tick it
    posts. Login matching is intentionally avoided because the
    orchestrator PAT may be shared with a human reviewer's account."""

    def test_bot_authored_comment_is_filtered(self) -> None:
        # A Dependabot-style comment must NOT affect the hash even
        # though its body is unique and its id is not tracked.
        human = support.FakeComment(
            id=support._BOT_FILTER_HUMAN_COMMENT_ID,
            body="real human comment",
            user=support.FakeUser(support.TRUSTED_AUTHOR),
        )
        bot_comment = support.FakeComment(
            id=support._BOT_FILTER_BOT_COMMENT_ID,
            body="Bumps `requests` from 2.31.0 to 2.32.0",
            user=support.FakeUser("dependabot[bot]", type="Bot"),
        )
        issue_with_just_human = support.make_issue(1, comments=[human])
        issue_with_bot = support.make_issue(1, comments=[human, bot_comment])
        self.assertEqual(
            workflow._compute_user_content_hash(
                issue_with_just_human, set()
            ),
            workflow._compute_user_content_hash(
                issue_with_bot, set()
            ),
        )

    def test_user_type_human_still_contributes(self) -> None:
        # A regular human user's `type == "User"` must NOT be filtered.
        comment = support.FakeComment(
            id=support._TYPED_HUMAN_COMMENT_ID,
            body="adds an acceptance criterion",
            user=support.FakeUser(support.TRUSTED_AUTHOR, type="User"),
        )
        empty = support.make_issue(1)
        with_human = support.make_issue(1, comments=[comment])
        self.assertNotEqual(
            workflow._compute_user_content_hash(empty, set()),
            workflow._compute_user_content_hash(with_human, set()),
        )


class DriftAckRequiresExplicitMarkerTest(unittest.TestCase):
    """Reviewer point: a generic non-empty no-commit response is OFTEN a
    clarification question, not an ack. Only an explicit `ACK: ...`
    marker should be treated as acknowledgement; everything else parks
    awaiting human via `_on_question`."""

    def test_explicit_ack_marker_extracts_reason(self) -> None:
        msg = (
            "I reviewed the change.\n\n"
            "ACK: existing tests already cover the new requirement"
        )
        self.assertEqual(
            workflow._drift_ack_reason(msg),
            "existing tests already cover the new requirement",
        )

    def test_ack_is_case_insensitive_and_last_wins(self) -> None:
        # Case insensitive (mirrors VERDICT parsing) and the LAST marker
        # wins so a sample/template `ACK:` quoted earlier in the message
        # doesn't override the agent's real concluding marker.
        msg = (
            "I considered ack: stale-template-text but on re-reading\n\n"
            "ack: real final justification"
        )
        self.assertEqual(
            workflow._drift_ack_reason(msg),
            "real final justification",
        )

    def test_no_marker_returns_none(self) -> None:
        # Generic "satisfied" prose without the marker is NOT an ack.
        # `_post_user_content_change_result` parks via `_on_question`
        # on this branch so a real question isn't swallowed.
        msg = "Existing code already covers this; no change needed."
        self.assertIsNone(workflow._drift_ack_reason(msg))

    def test_clarification_question_returns_none(self) -> None:
        msg = "Should I also handle the empty-input case?"
        self.assertIsNone(workflow._drift_ack_reason(msg))
