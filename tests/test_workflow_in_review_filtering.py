# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Tests for the same-account human-comment filter and the cross-namespace filter on inline / review-summary feedback."""
from __future__ import annotations

import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

os.environ.setdefault("ORCHESTRATOR_SKIP_DOTENV", "1")

from orchestrator import config, workflow

from tests.fakes import (
    FakeComment,
    FakeGitHubClient,
    FakeLabel,
    FakePR,
    FakePRRef,
    FakePRReview,
    FakeUser,
    make_issue,
)
from tests.workflow_helpers import (
    _PatchedWorkflowMixin,
    _TEST_SPEC,
    _agent,
)


class SameAccountHumanFeedbackTest(unittest.TestCase, _PatchedWorkflowMixin):
    """Operators commonly run the orchestrator with a personal PAT and also
    review PRs by hand from that same GitHub account. The self-comment filter
    must not key on author login -- if it did, real human review feedback from
    that account would be dropped as bot noise and the fixing route would
    silently swallow the human's 'please do not merge' comment.

    The fix tracks orchestrator-authored comments by exact id (recorded when
    the orchestrator posts them via `_post_issue_comment` /
    `_post_pr_comment`). A human comment from the PAT login carries an id the
    orchestrator never recorded, so it surfaces as fresh PR feedback and
    routes to `fixing`.
    """

    PR_NUMBER = 200
    BRANCH = "orchestrator/issue-100"

    def test_same_account_human_pr_comment_routes_to_fixing(self) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(100, label="in_review")
        gh.add_issue(issue)
        long_ago = datetime.now(timezone.utc) - timedelta(hours=1)
        # The orchestrator's previous park message and the human's "please do
        # not merge yet" comment are both authored by FakeUser("orchestrator")
        # -- this models the operator's personal PAT being used both for the
        # bot and for the human review. Only the park id is in the recorded
        # set; the human comment must surface as fresh feedback.
        pr = FakePR(
            number=self.PR_NUMBER, head_branch=self.BRANCH,
            head=FakePRRef(sha="cafe1234"),
            mergeable=True, check_state="success",
            issue_comments=[
                FakeComment(
                    id=3000, body="please do not merge yet",
                    user=FakeUser("orchestrator"),  # same login as PAT owner
                    created_at=long_ago,
                ),
            ],
        )
        gh.add_pr(pr)
        gh.seed_state(
            100,
            pr_number=self.PR_NUMBER,
            branch=self.BRANCH,
            dev_agent="claude",
            dev_session_id="dev-sess",
            # Watermark just past the orchestrator's earlier comments and the
            # human's id-3000 comment. Filter must drop only ids the
            # orchestrator actually recorded.
            pr_last_comment_id=2999,
            orchestrator_comment_ids=[900, 901],
            pickup_comment_id=900,
        )

        with patch.object(config, "IN_REVIEW_DEBOUNCE_SECONDS", 600):
            mocks = self._run(
                lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
                run_agent=_agent(),
            )

        # No merge (humans drive the merge), and the human's standing
        # objection routes the issue to `fixing`.
        self.assertEqual(gh.merge_calls, [])
        self.assertNotIn((100, "done"), gh.label_history)
        # The human comment is treated as fresh feedback and routes the
        # issue to `fixing` -- the dev session is not spawned here; the
        # fixing handler owns that step.
        mocks["run_agent"].assert_not_called()
        self.assertIn((100, "fixing"), gh.label_history)
        self.assertEqual(
            gh.pinned_data(100).get("pending_fix_issue_max_id"), 3000,
        )

    def test_same_account_human_issue_comment_at_handoff_is_preserved(self) -> None:
        # Validating-handoff variant: a human posts a review comment on the
        # issue thread (under the same account that owns the PAT) while
        # validating is still running. Without the id-based filter, the
        # handoff would advance the watermark past the human comment as if
        # it were the orchestrator's own self-run, then silently swallow
        # it on the next in_review tick.
        gh = FakeGitHubClient()
        long_ago = datetime.now(timezone.utc) - timedelta(hours=1)
        issue = make_issue(101, label="validating", comments=[
            FakeComment(
                id=900, body=":robot: orchestrator picking this up.",
                user=FakeUser("orchestrator"),  # PAT-owner login
                created_at=long_ago,
            ),
            FakeComment(
                id=901, body=":sparkles: PR opened: #210",
                user=FakeUser("orchestrator"),
                created_at=long_ago,
            ),
            # Human review feedback posted from the same account during
            # validating. Login alone cannot distinguish this from the bot's
            # own messages; only the recorded-id set can.
            FakeComment(
                id=950, body="please add a docstring",
                user=FakeUser("orchestrator"),  # same login as PAT owner
                created_at=long_ago,
            ),
        ])
        gh.add_issue(issue)
        pr = FakePR(
            number=210, head_branch="orchestrator/issue-101",
            head=FakePRRef(sha="cafe1234"),
            mergeable=True, check_state="success",
        )
        gh.add_pr(pr)
        gh.seed_state(
            101, pr_number=210, branch="orchestrator/issue-101",
            dev_agent="claude", dev_session_id="dev-sess",
            review_round=0,
            orchestrator_comment_ids=[900, 901],
            pickup_comment_id=900,
        )

        # Step 1: validating approves; watermark seed must STOP at id=950.
        self._run(
            lambda: workflow._handle_validating(gh, _TEST_SPEC, issue),
            run_agent=_agent(last_message="LGTM\n\nVERDICT: APPROVED"),
        )
        wm = gh.pinned_data(101).get("pr_last_comment_id")
        self.assertIsNotNone(wm)
        self.assertLess(
            wm, 950,
            f"watermark must stop before same-account human comment id=950 "
            f"(got {wm})",
        )

        # Step 2: in_review tick. Human comment is still past the watermark
        # and the in_review handler hands it off to `fixing` (no inline
        # dev resume).
        if not any(l.name == "in_review" for l in issue.labels):
            issue.labels = [FakeLabel("in_review")]
        with patch.object(config, "IN_REVIEW_DEBOUNCE_SECONDS", 600):
            mocks = self._run(
                lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
                run_agent=_agent(),
            )

        mocks["run_agent"].assert_not_called()
        self.assertEqual(gh.merge_calls, [])
        self.assertIn((101, "fixing"), gh.label_history)
        self.assertEqual(
            gh.pinned_data(101).get("pending_fix_issue_max_id"), 950,
        )


class OrchestratorMarkerFeedbackFilterTest(
    unittest.TestCase, _PatchedWorkflowMixin,
):
    """The in_review fresh-feedback scan must filter orchestrator-authored
    issue / PR-conversation comments by hidden body marker as well as by
    recorded id.

    Live state can miss a PR-conversation id (for example, a pre-marker or
    failed state-write window), and the bounded id list can eventually evict
    older entries. The marker is durable on the GitHub comment, so the scan
    must not route a marked bot comment to `fixing`.
    """

    def test_marked_pr_comment_missing_recorded_id_does_not_route_to_fixing(
        self,
    ) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(120, label="in_review")
        gh.add_issue(issue)
        long_ago = datetime.now(timezone.utc) - timedelta(hours=1)
        pr = FakePR(
            number=220,
            head_branch="orchestrator/issue-120",
            head=FakePRRef(sha="ready-sha"),
            mergeable=True,
            check_state="success",
            issue_comments=[
                FakeComment(
                    id=3000,
                    body=(
                        ":eyes: codex review requested changes\n\n"
                        f"{workflow._ORCH_COMMENT_MARKER}"
                    ),
                    user=FakeUser("orchestrator"),
                    created_at=long_ago,
                ),
            ],
        )
        gh.add_pr(pr)
        gh.seed_state(
            120,
            pr_number=220,
            branch="orchestrator/issue-120",
            dev_agent="claude",
            dev_session_id="dev-sess",
            pr_last_comment_id=2999,
            pr_last_review_comment_id=0,
            pr_last_review_summary_id=0,
            # Deliberately omit id=3000 to model stale / incomplete state.
            orchestrator_comment_ids=[900, 901],
            docs_checked_sha="ready-sha",
            docs_verdict="no_change",
        )

        with patch.object(config, "IN_REVIEW_DEBOUNCE_SECONDS", 600):
            mocks = self._run(
                lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
                run_agent=_agent(),
            )

        mocks["run_agent"].assert_not_called()
        self.assertNotIn((120, "fixing"), gh.label_history)
        self.assertEqual(gh.merge_calls, [])
        self.assertEqual(gh.pinned_data(120).get("ready_ping_sha"), "ready-sha")
        self.assertTrue(any(
            "ready for review/merge" in body
            for _, body in gh.posted_comments
        ))


class CrossNamespaceFilterTest(unittest.TestCase, _PatchedWorkflowMixin):
    """orchestrator_comment_ids records ids from the IssueComment namespace
    only. Inline review comments and PR review summaries live in different
    id namespaces, where numeric collisions with recorded bot comment ids
    are possible -- and any human inline / summary feedback that happens to
    share an id must NOT be filtered out as self-authored.
    """

    def test_inline_review_with_colliding_id_still_surfaces(self) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(160, label="in_review")
        gh.add_issue(issue)
        long_ago = datetime.now(timezone.utc) - timedelta(hours=1)
        pr = FakePR(
            number=400, head_branch="orchestrator/issue-160",
            head=FakePRRef(sha="cafe1234"),
            mergeable=True, check_state="success",
            review_comments=[
                FakeComment(
                    id=4242, body="rename foo to bar",
                    user=FakeUser("alice"), created_at=long_ago,
                ),
            ],
        )
        gh.add_pr(pr)
        # Bot id 4242 was recorded in the issue-side namespace (e.g. the
        # validating handoff approval comment landed there with that id).
        # The same numeric id on the inline-review surface is a different
        # object -- the filter must ignore the namespace collision.
        gh.seed_state(
            160, pr_number=400, branch="orchestrator/issue-160",
            dev_agent="claude", dev_session_id="dev-sess",
            pr_last_comment_id=4242,
            pr_last_review_comment_id=4241,
            pr_last_review_summary_id=0,
            orchestrator_comment_ids=[4242],
        )

        with patch.object(config, "IN_REVIEW_DEBOUNCE_SECONDS", 600):
            mocks = self._run(
                lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
                run_agent=_agent(),
            )

        # Inline review comment id=4242 surfaces despite colliding with
        # the recorded IssueComment id 4242; the handler routes to
        # `fixing` instead.
        mocks["run_agent"].assert_not_called()
        self.assertEqual(gh.merge_calls, [])
        self.assertIn((160, "fixing"), gh.label_history)
        self.assertEqual(
            gh.pinned_data(160).get("pending_fix_review_max_id"), 4242,
        )

    def test_review_summary_with_colliding_id_still_surfaces(self) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(161, label="in_review")
        gh.add_issue(issue)
        long_ago = datetime.now(timezone.utc) - timedelta(hours=1)
        pr = FakePR(
            number=401, head_branch="orchestrator/issue-161",
            head=FakePRRef(sha="cafe1234"),
            mergeable=True, check_state="success",
            reviews=[
                FakePRReview(
                    id=5000, body="please tighten the spec",
                    state="COMMENTED",
                    user=FakeUser("alice"),
                    submitted_at=long_ago,
                    commit_id="cafe1234",
                ),
            ],
        )
        gh.add_pr(pr)
        gh.seed_state(
            161, pr_number=401, branch="orchestrator/issue-161",
            dev_agent="claude", dev_session_id="dev-sess",
            pr_last_comment_id=5000,
            pr_last_review_comment_id=0,
            pr_last_review_summary_id=4999,
            orchestrator_comment_ids=[5000],
        )

        with patch.object(config, "IN_REVIEW_DEBOUNCE_SECONDS", 600):
            mocks = self._run(
                lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
                run_agent=_agent(),
            )

        mocks["run_agent"].assert_not_called()
        self.assertEqual(gh.merge_calls, [])
        self.assertIn((161, "fixing"), gh.label_history)
        self.assertEqual(
            gh.pinned_data(161).get("pending_fix_review_summary_max_id"),
            5000,
        )
