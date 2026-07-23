# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Workflow drift routing and acknowledgement tests."""
from __future__ import annotations

import unittest

from unittest.mock import patch

from orchestrator import config, workflow

from tests import workflow_drift_test_support as support


class HandlePickupInitializesUserContentHashTest(
    unittest.TestCase, support._PatchedWorkflowMixin,
):
    def test_pickup_with_decompose_off_seeds_hash(self) -> None:
        gh = support.FakeGitHubClient()
        issue = support.make_issue(1)
        gh.add_issue(issue)

        with patch.object(config, "DECOMPOSE", False):
            self._run(
                lambda: workflow._handle_pickup(gh, support._TEST_SPEC, issue),
                run_agent=support._agent(last_message="q"),
                has_new_commits=False,
            )

        state = gh.pinned_data(1)
        self.assertIn(support.KEY_USER_CONTENT_HASH, state)
        # Hash filters the pickup comment by id (it has been recorded in
        # `orchestrator_comment_ids`), so it should match a re-computation
        # over the same set.
        orch_ids = set(state.get("orchestrator_comment_ids") or [])
        self.assertEqual(
            state[support.KEY_USER_CONTENT_HASH],
            workflow._compute_user_content_hash(issue, orch_ids),
        )

    def test_pickup_with_decompose_on_seeds_hash(self) -> None:
        gh = support.FakeGitHubClient()
        issue = support.make_issue(2)
        gh.add_issue(issue)

        with patch.object(config, "DECOMPOSE", True):
            self._run(
                lambda: workflow._handle_pickup(gh, support._TEST_SPEC, issue),
                run_agent=support._agent(
                    session_id="dec-sess",
                    last_message=(
                        "fits one\n\n```orchestrator-manifest\n"
                        '{"decision": "single", "rationale": "small"}\n'
                        "```"
                    ),
                ),
            )

        state = gh.pinned_data(2)
        self.assertIn(support.KEY_USER_CONTENT_HASH, state)


class UserContentChangePromptIncludesCommentsTest(unittest.TestCase):
    """A drift triggered by a NEW human comment (not a body edit) must
    surface that comment to the dev. Quoting only title/body would leave
    the dev unaware of the acceptance criterion the human just posted."""

    def test_recent_comments_quoted_in_resume_prompt(self) -> None:
        issue = support.make_issue(1, title="t", body="b")
        issue.comments.append(support.FakeComment(
            id=support._PROMPT_COMMENT_ID,
            body="new acceptance criterion: handle empty input",
            user=support.FakeUser(support.TRUSTED_AUTHOR),
        ))
        comments_text = workflow._recent_comments_text(issue)
        prompt = workflow._build_user_content_change_prompt(
            issue, comments_text,
        )
        self.assertIn("new acceptance criterion", prompt)
        self.assertIn("Conversation so far", prompt)
        self.assertIn("Updated issue body", prompt)


class FirstTimeHashSeedingIsDurableTest(
    unittest.TestCase, support._PatchedWorkflowMixin,
):
    """Reviewer point 3: `_detect_user_content_change` must persist the
    first-time baseline via `gh.write_pinned_state` immediately, so a
    later edit after a parked/idle tick is not silently absorbed as the
    new baseline."""

    def test_validating_no_reply_persists_baseline(
        self,
    ) -> None:
        # Legacy state (no `user_content_hash`) parked on awaiting_human
        # with no new comments. `_handle_validating`'s awaiting-human
        # path returns without writing state on a real no-reply tick;
        # the durability fix in `_detect_user_content_change` must still
        # have written the baseline by then.
        gh = support.FakeGitHubClient()
        issue = support.make_issue(100, label=support.LABEL_VALIDATING, body="initial body")
        gh.add_issue(issue)
        pr = support.FakePR(number=1000, head_branch="orchestrator/geserdugarov__agent-orchestrator/issue-100")
        gh.add_pr(pr)
        gh.seed_state(
            100,
            pr_number=pr.number,
            awaiting_human=True,
            last_action_comment_id=support._INITIAL_LAST_ACTION_COMMENT_ID,
            review_round=1,
        )

        # Tick: no new comments and no hash baseline. Park branch
        # returns early without writing state.
        self._run(
            lambda: workflow._handle_validating(gh, support._TEST_SPEC, issue),
            run_agent=support._agent(),
        )

        # Baseline durably persisted by the first-call branch in
        # `_detect_user_content_change`.
        state = gh.pinned_data(100)
        self.assertIsNotNone(state.get(support.KEY_USER_CONTENT_HASH))

    def test_blocked_child_no_op_persists_baseline(self) -> None:
        # A `blocked` child waiting on a sibling is a per-tick no-op.
        # Without the durability fix, a later edit during the wait would
        # silently become the new baseline because the no-op branch
        # returns without `write_pinned_state`.
        gh = support.FakeGitHubClient()
        child = support.make_issue(
            support._BLOCKED_CHILD_ISSUE_NUMBER,
            label=support.LABEL_BLOCKED,
            body="child body",
        )
        gh.add_issue(child)
        gh.seed_state(
            support._BLOCKED_CHILD_ISSUE_NUMBER,
            parent_number=support._BLOCKED_PARENT_ISSUE_NUMBER,
        )

        self._run(
            lambda: workflow._handle_blocked(gh, support._TEST_SPEC, child),
            run_agent=support._agent(),
        )

        state = gh.pinned_data(support._BLOCKED_CHILD_ISSUE_NUMBER)
        self.assertIsNotNone(state.get(support.KEY_USER_CONTENT_HASH))


class NoCommitAckDoesNotParkTest(
    unittest.TestCase, support._PatchedWorkflowMixin,
):
    """Reviewer point 4: a harmless clarification edit can elicit a
    no-commit reply from the dev ('existing work satisfies'). The
    validating / in_review / resolving_conflict drift paths must treat
    that as an ack rather than parking awaiting_human."""

    def test_validating_ack_does_not_park(self) -> None:
        gh = support.FakeGitHubClient()
        issue = support.make_issue(
            support._VALIDATING_ACK_ISSUE_NUMBER,
            label=support.LABEL_VALIDATING,
            body=support.CLARIFIED_BODY,
        )
        gh.add_issue(issue)
        pr = support.FakePR(
            number=support._VALIDATING_ACK_PR_NUMBER,
            head_branch="orchestrator/geserdugarov__agent-orchestrator/issue-600",
        )
        gh.add_pr(pr)
        gh.seed_state(
            support._VALIDATING_ACK_ISSUE_NUMBER,
            pr_number=pr.number,
            dev_agent=support.BACKEND_CLAUDE,
            dev_session_id=support.DEV_SESSION,
            user_content_hash=support.STALE_HASH,
            review_round=1,
            branch="orchestrator/geserdugarov__agent-orchestrator/issue-600",
        )

        self._run(
            lambda: workflow._handle_validating(gh, support._TEST_SPEC, issue),
            run_agent=support._agent(
                session_id=support.DEV_SESSION,
                last_message=(
                    "Reviewed the clarified body.\n\n"
                    "ACK: existing commits already cover the clarified body"
                ),
            ),
            has_new_commits=False,
            dirty_files=(),
            head_shas=[support.SAME_SHA, support.SAME_SHA],
        )

        state = gh.pinned_data(support._VALIDATING_ACK_ISSUE_NUMBER)
        # Crucial: must NOT park as a question.
        self.assertFalse(state.get(support.KEY_AWAITING_HUMAN))
        # Dev's ACK justification was posted on the issue as an FYI.
        self.assertTrue(any(
            support.EXISTING_WORK_MESSAGE in body
            for _, body in gh.posted_comments
        ))

    def test_in_review_ack_routes_to_validating(
        self,
    ) -> None:
        # A no-commit "ack" reply from the dev on an in_review drift
        # MUST bounce DIRECTLY back to `validating` (same destination
        # as the pushed-fix exit; docs do not run on the drift exit,
        # the single docs pass runs after reviewer approval before
        # `in_review` via the final-docs handoff). `review_round`
        # resets so the validating cap counts fresh rounds.
        gh = support.FakeGitHubClient()
        issue = support.make_issue(
            support._IN_REVIEW_ACK_ISSUE_NUMBER,
            label=support.LABEL_IN_REVIEW,
            body=support.CLARIFIED_BODY,
        )
        gh.add_issue(issue)
        pr = support.FakePR(
            number=support._IN_REVIEW_ACK_PR_NUMBER,
            head_branch="orchestrator/geserdugarov__agent-orchestrator/issue-700",
        )
        gh.add_pr(pr)
        gh.seed_state(
            support._IN_REVIEW_ACK_ISSUE_NUMBER,
            pr_number=pr.number,
            dev_agent=support.BACKEND_CLAUDE,
            dev_session_id=support.DEV_SESSION,
            user_content_hash=support.STALE_HASH,
            pr_last_comment_id=0,
            pr_last_review_comment_id=0,
            pr_last_review_summary_id=0,
            branch="orchestrator/geserdugarov__agent-orchestrator/issue-700",
        )

        self._run(
            lambda: workflow._handle_in_review(gh, support._TEST_SPEC, issue),
            run_agent=support._agent(
                session_id=support.DEV_SESSION,
                last_message="ACK: no additional code change needed",
            ),
            has_new_commits=False,
            dirty_files=(),
            head_shas=[support.UNCHANGED_SHA, support.UNCHANGED_SHA],
        )

        state = gh.pinned_data(support._IN_REVIEW_ACK_ISSUE_NUMBER)
        # Must NOT park (the dev acknowledged, not asked a question).
        self.assertFalse(state.get(support.KEY_AWAITING_HUMAN))
        # MUST bounce directly to validating (no documenting hop) so
        # the reviewer re-evaluates against the updated body.
        self.assertIn(
            (support._IN_REVIEW_ACK_ISSUE_NUMBER, support.LABEL_VALIDATING),
            gh.label_history,
        )
        # And NOT through documenting -- no commit landed.
        self.assertNotIn(
            (support._IN_REVIEW_ACK_ISSUE_NUMBER, "documenting"),
            gh.label_history,
        )
        # review_round reset so the validating cap counts fresh rounds.
        self.assertEqual(state.get("review_round"), 0)
        # Dev's reply still posted on the issue as an FYI.
        self.assertTrue(any(
            support.EXISTING_WORK_MESSAGE in body
            for _, body in gh.posted_comments
        ))
