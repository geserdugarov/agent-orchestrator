# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import unittest

from orchestrator import workflow

from tests.fakes import (
    FakeComment,
    FakeGitHubClient,
    FakeUser,
    make_issue,
)
from tests.workflow_helpers import (
    _PatchedWorkflowMixin,
    _TEST_SPEC,
    _agent,
)


class HandleReadyTest(unittest.TestCase, _PatchedWorkflowMixin):
    def test_routes_to_implementing_same_tick(self) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(20, label="ready")
        gh.add_issue(issue)

        mocks = self._run(
            lambda: workflow._handle_ready(gh, _TEST_SPEC, issue),
            run_agent=_agent(
                session_id="dev-sess", last_message="implemented"
            ),
            has_new_commits=[False, True],
            push_branch=True,
        )

        # Label flips to implementing on the same tick; the dev agent ran
        # and a PR opened.
        self.assertEqual(gh.label_history[0], (20, "implementing"))
        mocks["run_agent"].assert_called_once()
        self.assertEqual(len(gh.opened_prs), 1)
        # pickup_comment_id seeded so the validating handoff can anchor
        # the in_review watermark seed on it.
        state = gh.pinned_data(20)
        self.assertIn("pickup_comment_id", state)
        self.assertIn("created_at", state)

    def test_handle_ready_keeps_existing_pickup_state(self) -> None:
        # If pickup state was already seeded (e.g. by a re-tick after the
        # legacy pickup path), don't double-post the picking-this-up
        # comment.
        gh = FakeGitHubClient()
        issue = make_issue(21, label="ready")
        gh.add_issue(issue)
        gh.seed_state(
            21,
            pickup_comment_id=500,
            created_at="2026-05-03T00:00:00+00:00",
        )

        before = len(gh.posted_comments)
        self._run(
            lambda: workflow._handle_ready(gh, _TEST_SPEC, issue),
            run_agent=_agent(
                session_id="dev-sess", last_message="done"
            ),
            has_new_commits=[False, True],
            push_branch=True,
        )

        # The "picking this up; starting implementation" comment was NOT
        # re-posted. (`_on_commits` still posts a `:sparkles:` comment.)
        new_comments = gh.posted_comments[before:]
        self.assertFalse(any(
            "picking this up" in body for _, body in new_comments
        ))

    def test_marks_prior_comments_consumed(self) -> None:
        # A parent that came through `decomposing` -> `blocked` ->
        # all-children-done -> `ready` carries a `pickup_comment_id`
        # anchored on the original "decomposing" comment. Any human
        # feedback posted while children were resolving sits at a
        # comment id ABOVE pickup, so the in_review watermark seed
        # would classify it as post-pickup unconsumed PR feedback and
        # bounce the PR back to validating after the implementer has
        # already incorporated it. _handle_ready must bump
        # `last_action_comment_id` past the latest visible comment so
        # `_seed_watermark_past_self`'s `consumed_through` walk treats
        # those decomposing/blocked-era comments as already-fed-to-the-dev.
        gh = FakeGitHubClient()
        issue = make_issue(22, label="ready")
        # Decomposing-era human comment -- id well above the original
        # pickup comment id.
        issue.comments.append(FakeComment(
            id=2050, body="please use snake_case",
            user=FakeUser("alice"),
        ))
        gh.add_issue(issue)
        gh.seed_state(
            22,
            pickup_comment_id=500,
            created_at="2026-05-03T00:00:00+00:00",
        )

        self._run(
            lambda: workflow._handle_ready(gh, _TEST_SPEC, issue),
            run_agent=_agent(
                session_id="dev-sess", last_message="done"
            ),
            has_new_commits=[False, True],
            push_branch=True,
        )

        state = gh.pinned_data(22)
        last_action = state.get("last_action_comment_id")
        self.assertIsNotNone(
            last_action,
            "last_action_comment_id must be set so the in_review "
            "handoff treats decomposing-era comments as consumed",
        )
        self.assertGreaterEqual(int(last_action), 2050)

    def test_keeps_existing_last_action(self) -> None:
        # If a prior decomposing park already advanced
        # `last_action_comment_id` past everything, _handle_ready must
        # not regress it. Latest comment id might be smaller than the
        # park id when the latest is the orchestrator's own pinned-state
        # comment from a fresh seed (low id) and the prior park id was
        # higher.
        gh = FakeGitHubClient()
        issue = make_issue(23, label="ready")
        gh.add_issue(issue)
        gh.seed_state(
            23,
            pickup_comment_id=500,
            last_action_comment_id=9999,
            created_at="2026-05-03T00:00:00+00:00",
        )

        self._run(
            lambda: workflow._handle_ready(gh, _TEST_SPEC, issue),
            run_agent=_agent(
                session_id="dev-sess", last_message="done"
            ),
            has_new_commits=[False, True],
            push_branch=True,
        )

        state = gh.pinned_data(23)
        self.assertGreaterEqual(int(state["last_action_comment_id"]), 9999)
