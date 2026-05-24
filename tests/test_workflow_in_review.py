# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

os.environ.setdefault("ORCHESTRATOR_SKIP_DOTENV", "1")

from orchestrator import config, workflow
from orchestrator.github import BASE_SYNC_HOLD_LABEL

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


class HandleInReviewTest(unittest.TestCase, _PatchedWorkflowMixin):
    """Drive the in_review handler through merged / closed-not-merged /
    open-PR (auto-merge gates and PR-comment debounce) branches against a
    seeded FakePR.
    """

    PR_NUMBER = 77
    BRANCH = "orchestrator/issue-30"

    def _seed(
        self,
        *,
        issue_number: int = 30,
        pr=None,
        with_pr_number: bool = True,
        extra_state=None,
    ):
        gh = FakeGitHubClient()
        issue = make_issue(issue_number, label="in_review")
        gh.add_issue(issue)
        if pr is not None:
            gh.add_pr(pr)
        state: dict = {
            "branch": self.BRANCH,
            "dev_agent": "claude",
            "dev_session_id": "dev-sess",
            "review_round": 1,
        }
        if with_pr_number and pr is not None:
            state["pr_number"] = pr.number
        if extra_state:
            state.update(extra_state)
        gh.seed_state(issue_number, **state)
        return gh, issue

    def _open_pr(self, **kwargs):
        defaults = dict(
            number=self.PR_NUMBER,
            head_branch=self.BRANCH,
            head=FakePRRef(sha="cafe1234"),
        )
        defaults.update(kwargs)
        return FakePR(**defaults)

    def test_in_review_pr_merged_externally(self) -> None:
        pr = self._open_pr(merged=True, state="closed")
        gh, issue = self._seed(pr=pr)

        mocks = self._run(
            lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
            run_agent=_agent(),
        )

        self.assertIn((30, "done"), gh.label_history)
        self.assertIn("merged_at", gh.pinned_data(30))
        self.assertTrue(issue.closed)
        self.assertEqual(gh.merge_calls, [])
        # Branch cleanup must fire for an external merge: the PR is gone, so
        # the per-issue worktree and the local + remote branches are dead
        # weight that should not survive past the `done` flip.
        mocks["_cleanup_terminal_branch"].assert_called_once_with(
            gh, _TEST_SPEC, 30,
        )

    def test_in_review_pr_closed_unmerged(self) -> None:
        pr = self._open_pr(merged=False, state="closed")
        gh, issue = self._seed(pr=pr)

        mocks = self._run(
            lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
            run_agent=_agent(),
        )

        self.assertIn((30, "rejected"), gh.label_history)
        self.assertIn("closed_without_merge_at", gh.pinned_data(30))
        self.assertTrue(issue.closed)
        self.assertEqual(gh.merge_calls, [])
        # The PR is gone, so the orchestrator-owned branch and worktree
        # are dead weight regardless of whether the PR merged or was
        # declined. Cleanup must fire on the rejected terminal too.
        mocks["_cleanup_terminal_branch"].assert_called_once_with(
            gh, _TEST_SPEC, 30,
        )

    def test_in_review_auto_merge_off_mergeable_pings_human(self) -> None:
        # AUTO_MERGE off + PR mergeable: post a one-shot HITL ping so the
        # human knows the PR is ready, but stay open (no merge, no label
        # flip, no awaiting_human). The ping must mention every HITL handle
        # so notifications fire even on a multi-handle deployment.
        pr = self._open_pr(approved=True, mergeable=True, check_state="success")
        gh, issue = self._seed(pr=pr)

        with patch.object(config, "AUTO_MERGE", False), \
             patch.object(config, "HITL_MENTIONS", "@alice @bob"):
            mocks = self._run(
                lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
                run_agent=_agent(),
            )

        mocks["run_agent"].assert_not_called()
        self.assertEqual(gh.merge_calls, [])
        self.assertEqual(gh.label_history, [])
        self.assertFalse(issue.closed)
        # Exactly one ping was posted on the issue thread.
        ping_comments = [
            body for _, body in gh.posted_comments
            if "ready for review/merge" in body
        ]
        self.assertEqual(len(ping_comments), 1)
        self.assertIn("@alice", ping_comments[0])
        self.assertIn("@bob", ping_comments[0])
        self.assertIn(f"PR #{self.PR_NUMBER}", ping_comments[0])
        data = gh.pinned_data(30)
        # De-dup key is the head SHA we pinged for.
        self.assertEqual(data.get("ready_ping_sha"), "cafe1234")
        # Not parked: subsequent ticks must still react to comments / state.
        self.assertFalse(data.get("awaiting_human"))
        # Ping is recorded in orchestrator_comment_ids so the next tick's
        # `comments_after` filter excludes it as bot noise without needing
        # the watermark to move (which would risk swallowing a human
        # comment that landed between the earlier scan and the ping).
        ping_id = gh.latest_comment_id(issue)
        self.assertIsNotNone(ping_id)
        self.assertIn(ping_id, data.get("orchestrator_comment_ids", []))

    def test_in_review_auto_merge_off_mergeable_dedups_same_head(self) -> None:
        # Second tick on the same head SHA must NOT re-ping; the ping is
        # one-shot per head so a long-lived ready-for-merge PR doesn't spam
        # the HITL handles on every poll.
        pr = self._open_pr(approved=True, mergeable=True, check_state="success")
        gh, issue = self._seed(pr=pr)

        with patch.object(config, "AUTO_MERGE", False):
            self._run(
                lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
                run_agent=_agent(),
            )
            comments_after_first = list(gh.posted_comments)
            self._run(
                lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
                run_agent=_agent(),
            )

        self.assertEqual(gh.posted_comments, comments_after_first)

    def test_in_review_auto_merge_off_mergeable_repings_new_head(self) -> None:
        # A new commit on the PR branch shifts pr.head.sha; the ping is
        # keyed on the SHA we last pinged for, so the next tick must
        # re-ping on the new head.
        pr = self._open_pr(approved=True, mergeable=True, check_state="success")
        gh, issue = self._seed(pr=pr)

        with patch.object(config, "AUTO_MERGE", False):
            self._run(
                lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
                run_agent=_agent(),
            )
            pings_first = [
                body for _, body in gh.posted_comments
                if "ready for review/merge" in body
            ]
            pr.head = FakePRRef(sha="beefcafe")
            self._run(
                lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
                run_agent=_agent(),
            )

        pings_total = [
            body for _, body in gh.posted_comments
            if "ready for review/merge" in body
        ]
        self.assertEqual(len(pings_first), 1)
        self.assertEqual(len(pings_total), 2)
        self.assertEqual(gh.pinned_data(30).get("ready_ping_sha"), "beefcafe")

    def test_in_review_ready_ping_does_not_swallow_concurrent_human(self) -> None:
        # Race window: a human posts an issue comment AFTER the handler's
        # comment scan but BEFORE the ready-for-merge ping. The ping must
        # NOT bump `pr_last_comment_id` past the unseen human comment;
        # otherwise the next tick's `comments_after` would skip the human
        # feedback and the dev would never resume on it.
        from unittest.mock import patch as _patch_mock
        long_ago = datetime.now(timezone.utc) - timedelta(hours=1)
        pr = self._open_pr(approved=True, mergeable=True, check_state="success")
        gh, issue = self._seed(
            pr=pr, extra_state={"pr_last_comment_id": 1500}
        )
        # Pre-seed the human comment with an id ABOVE the watermark but
        # BELOW the ping id (the fake comment-id counter starts at 1000,
        # so the next id allocated by `_post_issue_comment` will be the
        # one after this). We splice the comment in mid-handler via a
        # patch on `_post_issue_comment` so it lands AFTER the scan.
        human = FakeComment(
            id=1600, body="please hold off, doing one more pass",
            user=FakeUser("alice"), created_at=long_ago,
        )
        from orchestrator import workflow_messages
        real_post = workflow_messages._post_issue_comment

        def post_with_race(gh_arg, issue_arg, state_arg, body_arg):
            # Simulate a human comment landing right before our ping.
            if "ready for review/merge" in body_arg:
                issue_arg.comments.append(human)
            return real_post(gh_arg, issue_arg, state_arg, body_arg)

        with patch.object(config, "AUTO_MERGE", False), \
             _patch_mock.object(workflow, "_post_issue_comment", post_with_race):
            self._run(
                lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
                run_agent=_agent(),
            )

        # Watermark must NOT have advanced past the human comment.
        data = gh.pinned_data(30)
        self.assertLess(data.get("pr_last_comment_id"), human.id)

        # Second tick: the human comment surfaces and triggers a dev
        # resume (the ping is filtered as orchestrator-authored).
        mocks = self._run(
            lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
            run_agent=_agent(session_id="dev-sess", last_message="held"),
            push_branch=True,
            head_shas=["cafe1234", "deadbeef"],
        )
        self.assertEqual(mocks["run_agent"].call_count, 1)
        self.assertIn(
            "please hold off",
            mocks["run_agent"].call_args.args[1],
        )

    def test_in_review_auto_merge_happy_path(self) -> None:
        pr = self._open_pr(approved=True, mergeable=True, check_state="success")
        gh, issue = self._seed(pr=pr)

        with patch.object(config, "AUTO_MERGE", True):
            mocks = self._run(
                lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
                run_agent=_agent(),
            )

        self.assertEqual(gh.merge_calls, [(self.PR_NUMBER, "cafe1234", "squash")])
        self.assertIn((30, "done"), gh.label_history)
        self.assertIn("merged_at", gh.pinned_data(30))
        self.assertTrue(issue.closed)
        mocks["_cleanup_terminal_branch"].assert_called_once_with(
            gh, _TEST_SPEC, 30,
        )

    def test_in_review_hold_base_sync_pauses_auto_merge(self) -> None:
        pr = self._open_pr(approved=True, mergeable=True, check_state="success")
        gh, issue = self._seed(pr=pr)
        issue.labels.append(FakeLabel(BASE_SYNC_HOLD_LABEL))

        with patch.object(config, "AUTO_MERGE", True):
            mocks = self._run(
                lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
                run_agent=_agent(),
            )

        mocks["run_agent"].assert_not_called()
        self.assertEqual(gh.merge_calls, [])
        self.assertEqual(gh.label_history, [])
        self.assertNotIn("merged_at", gh.pinned_data(30))

    def test_in_review_auto_merge_blocked_on_pending_checks(self) -> None:
        pr = self._open_pr(approved=True, mergeable=True, check_state="pending")
        gh, issue = self._seed(pr=pr)

        with patch.object(config, "AUTO_MERGE", True):
            self._run(
                lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
                run_agent=_agent(),
            )

        self.assertEqual(gh.merge_calls, [])
        self.assertEqual(gh.label_history, [])
        self.assertEqual(gh.posted_comments, [])
        self.assertNotIn("merged_at", gh.pinned_data(30))

    def test_in_review_auto_merge_blocked_on_no_approval(self) -> None:
        pr = self._open_pr(approved=False, mergeable=True, check_state="success")
        gh, issue = self._seed(pr=pr)

        with patch.object(config, "AUTO_MERGE", True):
            self._run(
                lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
                run_agent=_agent(),
            )

        self.assertEqual(gh.merge_calls, [])
        self.assertEqual(gh.label_history, [])
        self.assertNotIn("merged_at", gh.pinned_data(30))

    def test_in_review_auto_merge_blocked_on_failed_checks(self) -> None:
        pr = self._open_pr(approved=True, mergeable=True, check_state="failure")
        gh, issue = self._seed(pr=pr)

        with patch.object(config, "AUTO_MERGE", True):
            self._run(
                lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
                run_agent=_agent(),
            )

        self.assertEqual(gh.merge_calls, [])
        self.assertTrue(gh.pinned_data(30).get("awaiting_human"))
        last_comment = gh.posted_comments[-1][1]
        self.assertIn("checks are 'failure'", last_comment)
        self.assertIn(f"PR #{self.PR_NUMBER}", last_comment)

    def test_in_review_auto_merge_unmergeable_routes_to_resolving_conflict(self) -> None:
        # AUTO_MERGE on + PR not mergeable: instead of parking awaiting
        # human, the orchestrator flips the label to `resolving_conflict`,
        # seeds a fresh `conflict_round` counter, and lets the dedicated
        # handler attempt an automated merge of the base branch.
        pr = self._open_pr(approved=True, mergeable=False, check_state="success")
        gh, issue = self._seed(pr=pr)

        with patch.object(config, "AUTO_MERGE", True):
            self._run(
                lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
                run_agent=_agent(),
            )

        self.assertEqual(gh.merge_calls, [])
        self.assertIn((30, "resolving_conflict"), gh.label_history)
        data = gh.pinned_data(30)
        self.assertFalse(data.get("awaiting_human"))
        self.assertEqual(data.get("conflict_round"), 0)
        # PR comment notifies that auto-resolution is being attempted.
        self.assertTrue(gh.posted_pr_comments)
        last_pr_comment = gh.posted_pr_comments[-1][1]
        self.assertIn("auto-resolution", last_pr_comment)

    def test_in_review_hold_base_sync_skips_unmergeable_route(self) -> None:
        pr = self._open_pr(approved=True, mergeable=False, check_state="success")
        gh, issue = self._seed(pr=pr)
        issue.labels.append(FakeLabel(BASE_SYNC_HOLD_LABEL))

        with patch.object(config, "AUTO_MERGE", True):
            mocks = self._run(
                lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
                run_agent=_agent(),
            )

        mocks["run_agent"].assert_not_called()
        self.assertEqual(gh.merge_calls, [])
        self.assertEqual(gh.posted_pr_comments, [])
        self.assertNotIn((30, "resolving_conflict"), gh.label_history)
        data = gh.pinned_data(30)
        self.assertFalse(data.get("awaiting_human"))
        self.assertNotIn("conflict_round", data)

    def test_in_review_unmergeable_preserves_existing_conflict_round(self) -> None:
        # A PR that already went through one auto-resolution round and
        # bounced back to `in_review` still unmergeable (e.g. branch
        # protection) must NOT have its conflict_round reset on re-entry.
        # Resetting would make `MAX_CONFLICT_ROUNDS` ineffective for the
        # branch-protection / out-of-date-base heuristic case.
        pr = self._open_pr(approved=True, mergeable=False, check_state="success")
        gh, issue = self._seed(pr=pr, extra_state={"conflict_round": 2})

        with patch.object(config, "AUTO_MERGE", True):
            self._run(
                lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
                run_agent=_agent(),
            )

        self.assertIn((30, "resolving_conflict"), gh.label_history)
        data = gh.pinned_data(30)
        # Counter preserved at 2, not reset to 0.
        self.assertEqual(data.get("conflict_round"), 2)

    def test_in_review_unmergeable_unapproved_does_not_route(self) -> None:
        # Resolving_conflict resumes / pushes dev work; routing an
        # unapproved PR there would push unreviewed merges past the
        # original gating that the old `unmergeable` park honored. The
        # approval gate must run BEFORE the unmergeable check.
        pr = self._open_pr(
            approved=False, mergeable=False, check_state="success",
        )
        gh, issue = self._seed(pr=pr)

        with patch.object(config, "AUTO_MERGE", True):
            mocks = self._run(
                lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
                run_agent=_agent(),
            )

        mocks["run_agent"].assert_not_called()
        self.assertEqual(gh.merge_calls, [])
        # No resolving_conflict relabel and no PR comment about
        # auto-resolution.
        self.assertNotIn((30, "resolving_conflict"), gh.label_history)
        self.assertEqual(gh.posted_pr_comments, [])
        data = gh.pinned_data(30)
        self.assertFalse(data.get("awaiting_human"))
        # No conflict_round seeded -- we never entered the route.
        self.assertNotIn("conflict_round", data)

    def test_in_review_unmergeable_changes_requested_does_not_route(self) -> None:
        # A standing human CHANGES_REQUESTED on the current head vetoes
        # the resolving_conflict route. Without this gate, the dev
        # session would resume and push merge work over the human's
        # objection.
        pr = self._open_pr(
            approved=True, mergeable=False, check_state="success",
            changes_requested=True,
        )
        gh, issue = self._seed(pr=pr)

        with patch.object(config, "AUTO_MERGE", True):
            mocks = self._run(
                lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
                run_agent=_agent(),
            )

        mocks["run_agent"].assert_not_called()
        self.assertEqual(gh.merge_calls, [])
        self.assertNotIn((30, "resolving_conflict"), gh.label_history)
        self.assertEqual(gh.posted_pr_comments, [])
        data = gh.pinned_data(30)
        self.assertFalse(data.get("awaiting_human"))
        self.assertNotIn("conflict_round", data)

    def test_in_review_auto_merge_off_unmergeable_parks_legacy(self) -> None:
        # Legacy fallback: AUTO_MERGE off + unmergeable parks awaiting
        # human with `park_reason="unmergeable"`. Operators who haven't
        # opted into AUTO_MERGE still get visibility into the unmergeable
        # state, and the existing transient-park recovery picks the issue
        # back up if AUTO_MERGE is later flipped on.
        pr = self._open_pr(approved=True, mergeable=False, check_state="success")
        gh, issue = self._seed(pr=pr)

        with patch.object(config, "AUTO_MERGE", False):
            mocks = self._run(
                lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
                run_agent=_agent(),
            )

        mocks["run_agent"].assert_not_called()
        self.assertEqual(gh.merge_calls, [])
        # AUTO_MERGE off must NOT route to resolving_conflict.
        self.assertNotIn((30, "resolving_conflict"), gh.label_history)
        data = gh.pinned_data(30)
        self.assertTrue(data.get("awaiting_human"))
        self.assertEqual(data.get("park_reason"), "unmergeable")
        last_comment = gh.posted_comments[-1][1]
        self.assertIn("not mergeable", last_comment)
        # AUTO_MERGE off does not seed the conflict_round budget.
        self.assertNotIn("conflict_round", data)

    def test_in_review_hold_base_sync_skips_auto_merge_off_park(self) -> None:
        pr = self._open_pr(approved=True, mergeable=False, check_state="success")
        gh, issue = self._seed(pr=pr)
        issue.labels.append(FakeLabel(BASE_SYNC_HOLD_LABEL))

        with patch.object(config, "AUTO_MERGE", False):
            mocks = self._run(
                lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
                run_agent=_agent(),
            )

        mocks["run_agent"].assert_not_called()
        self.assertEqual(gh.merge_calls, [])
        self.assertEqual(gh.posted_comments, [])
        self.assertEqual(gh.label_history, [])
        data = gh.pinned_data(30)
        self.assertFalse(data.get("awaiting_human"))
        self.assertIsNone(data.get("park_reason"))

    def test_in_review_auto_merge_mergeable_pending(self) -> None:
        # mergeable=None means GitHub is still computing. Don't merge, don't
        # park; the next tick re-checks once GitHub has decided.
        pr = self._open_pr(approved=True, mergeable=None, check_state="success")
        gh, issue = self._seed(pr=pr)

        with patch.object(config, "AUTO_MERGE", True):
            self._run(
                lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
                run_agent=_agent(),
            )

        self.assertEqual(gh.merge_calls, [])
        self.assertEqual(gh.label_history, [])
        self.assertEqual(gh.posted_comments, [])
        self.assertFalse(gh.pinned_data(30).get("awaiting_human"))

    def test_in_review_pr_comment_within_debounce(self) -> None:
        # A PR comment posted just now must NOT trigger a dev resume; the
        # human may still be typing more comments.
        now = datetime.now(timezone.utc)
        pr = self._open_pr(
            approved=True, mergeable=True, check_state="success",
            issue_comments=[
                FakeComment(
                    id=2000, body="please tighten the docstring",
                    user=FakeUser("alice"), created_at=now,
                ),
            ],
        )
        # Watermark just below the comment so it surfaces as fresh feedback.
        # An unset watermark would trip the legacy in_review migration and
        # mask this comment as already-consumed.
        gh, issue = self._seed(
            pr=pr, extra_state={"pr_last_comment_id": 1999}
        )

        with patch.object(config, "AUTO_MERGE", True), \
             patch.object(config, "IN_REVIEW_DEBOUNCE_SECONDS", 600):
            mocks = self._run(
                lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
                run_agent=_agent(),
            )

        # Within debounce: no agent spawn, no merge, no label flip.
        mocks["run_agent"].assert_not_called()
        self.assertEqual(gh.merge_calls, [])
        self.assertEqual(gh.label_history, [])

    def test_in_review_pr_comment_past_debounce(self) -> None:
        long_ago = datetime.now(timezone.utc) - timedelta(hours=1)
        pr = self._open_pr(
            issue_comments=[
                FakeComment(
                    id=2000, body="rename foo to bar",
                    user=FakeUser("alice"), created_at=long_ago,
                ),
            ],
        )
        gh, issue = self._seed(
            pr=pr, extra_state={"pr_last_comment_id": 1999}
        )

        mocks = self._run(
            lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
            run_agent=_agent(session_id="dev-sess", last_message="renamed"),
            push_branch=True,
            head_shas=["aaa", "bbb"],
        )

        # Dev resumed on the locked backend with the PR-comment text quoted
        # into the prompt; pushed; bounced back to validating with round=0.
        self.assertEqual(mocks["run_agent"].call_count, 1)
        call = mocks["run_agent"].call_args
        self.assertEqual(call.args[0], "claude")
        self.assertEqual(call.kwargs.get("resume_session_id"), "dev-sess")
        self.assertIn("rename foo to bar", call.args[1])

        mocks["_push_branch"].assert_called_once()
        self.assertIn((30, "validating"), gh.label_history)
        data = gh.pinned_data(30)
        self.assertEqual(data.get("review_round"), 0)
        self.assertEqual(data.get("pr_last_comment_id"), 2000)

    def test_in_review_sha_mismatch_on_merge(self) -> None:
        # merge_pr returning False (409 SHA mismatch / 405 / 422) leaves the
        # issue in_review for the next tick to retry; no park, no label flip.
        pr = self._open_pr(approved=True, mergeable=True, check_state="success")
        gh, issue = self._seed(pr=pr)
        gh.merge_returns_ok = False

        with patch.object(config, "AUTO_MERGE", True):
            self._run(
                lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
                run_agent=_agent(),
            )

        self.assertEqual(gh.merge_calls, [(self.PR_NUMBER, "cafe1234", "squash")])
        self.assertEqual(gh.label_history, [])
        self.assertFalse(gh.pinned_data(30).get("awaiting_human"))
        self.assertNotIn("merged_at", gh.pinned_data(30))
        self.assertFalse(issue.closed)

    def test_in_review_pr_number_missing(self) -> None:
        # Manually-relabeled in_review without a pinned PR -- park once.
        gh, issue = self._seed(pr=None, with_pr_number=False)

        self._run(
            lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
            run_agent=_agent(),
        )

        self.assertTrue(gh.pinned_data(30).get("awaiting_human"))
        last_comment = gh.posted_comments[-1][1]
        self.assertIn("without a pinned `pr_number`", last_comment)

        # A second tick with awaiting_human set must NOT re-park (no second
        # comment posted; comment count stays at 1).
        before = len(gh.posted_comments)
        self._run(
            lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
            run_agent=_agent(),
        )
        self.assertEqual(len(gh.posted_comments), before)

    def test_in_review_agent_approval_unlocks_auto_merge(self) -> None:
        # The reviewer agent posts an issue comment, not a real PR review,
        # so pr_is_approved (which inspects pr.get_reviews()) is False even
        # after the agent emits VERDICT: APPROVED. The validating handler
        # persists `agent_approved_sha` for the head it reviewed; that key
        # is what the in_review auto-merge gate keys on.
        pr = self._open_pr(
            approved=False, mergeable=True, check_state="success",
            head=FakePRRef(sha="cafe1234"),
        )
        gh, issue = self._seed(
            pr=pr,
            extra_state={"agent_approved_sha": "cafe1234"},
        )

        with patch.object(config, "AUTO_MERGE", True):
            self._run(
                lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
                run_agent=_agent(),
            )

        self.assertEqual(gh.merge_calls, [(self.PR_NUMBER, "cafe1234", "squash")])
        self.assertIn((30, "done"), gh.label_history)

    def test_in_review_stale_agent_approval_blocks_auto_merge(self) -> None:
        # If the head moved after the agent approved (e.g., a human force-
        # pushed) the snapshot SHA no longer matches and pr_is_approved is
        # also False -- nothing auto-merges. We don't park here either; the
        # next event (new comment / close / re-approval bouncing back
        # through validating) is what unsticks us.
        pr = self._open_pr(
            approved=False, mergeable=True, check_state="success",
            head=FakePRRef(sha="newhead99"),
        )
        gh, issue = self._seed(
            pr=pr,
            extra_state={"agent_approved_sha": "cafe1234"},
        )

        with patch.object(config, "AUTO_MERGE", True):
            self._run(
                lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
                run_agent=_agent(),
            )

        self.assertEqual(gh.merge_calls, [])
        self.assertEqual(gh.label_history, [])
        self.assertFalse(gh.pinned_data(30).get("awaiting_human"))


class HandleInReviewClosedIssueExternalMergeTest(
    unittest.TestCase, _PatchedWorkflowMixin
):
    """A human merge with `Resolves #N` auto-closes issue N before the
    orchestrator ticks. The closed-in_review sweep yields the issue and
    `_handle_in_review` must still flip the label to `done` and stamp
    `merged_at` -- otherwise the issue stays closed-but-`in_review` forever.
    """

    def test_external_merge_on_closed_issue_finalizes_to_done(self) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(40, label="in_review")
        issue.closed = True  # Resolves #N has already auto-closed it.
        gh.add_issue(issue)
        pr = FakePR(
            number=99, head_branch="orchestrator/issue-40",
            head=FakePRRef(sha="cafe1234"),
            merged=True, state="closed",
        )
        gh.add_pr(pr)
        gh.seed_state(40, pr_number=99, branch="orchestrator/issue-40")

        self._run(
            lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
            run_agent=_agent(),
        )

        self.assertIn((40, "done"), gh.label_history)
        self.assertIn("merged_at", gh.pinned_data(40))


class StaleHumanApprovalAutoMergeTest(unittest.TestCase, _PatchedWorkflowMixin):
    """A human APPROVED review on an older head must NOT unlock auto-merge
    when a newer commit was pushed without re-approval. Otherwise a
    contributor could push code AFTER the human approval and have the
    orchestrator merge it unreviewed.
    """

    def test_stale_human_approval_blocks_auto_merge(self) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(50, label="in_review")
        gh.add_issue(issue)
        pr = FakePR(
            number=88, head_branch="orchestrator/issue-50",
            head=FakePRRef(sha="newhead"),
            approved=True,                  # human approved
            approval_head_sha="oldhead",    # ...but on the previous commit
            mergeable=True, check_state="success",
        )
        gh.add_pr(pr)
        gh.seed_state(50, pr_number=88, branch="orchestrator/issue-50")

        with patch.object(config, "AUTO_MERGE", True):
            self._run(
                lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
                run_agent=_agent(),
            )

        # No merge: stale approval is treated as missing.
        self.assertEqual(gh.merge_calls, [])
        self.assertEqual(gh.label_history, [])
        self.assertFalse(gh.pinned_data(50).get("awaiting_human"))

    def test_current_head_human_approval_allows_auto_merge(self) -> None:
        # Same setup but approval IS for the current head -- merge proceeds.
        gh = FakeGitHubClient()
        issue = make_issue(51, label="in_review")
        gh.add_issue(issue)
        pr = FakePR(
            number=89, head_branch="orchestrator/issue-51",
            head=FakePRRef(sha="newhead"),
            approved=True, approval_head_sha="newhead",
            mergeable=True, check_state="success",
        )
        gh.add_pr(pr)
        gh.seed_state(51, pr_number=89, branch="orchestrator/issue-51")

        with patch.object(config, "AUTO_MERGE", True):
            self._run(
                lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
                run_agent=_agent(),
            )

        self.assertEqual(gh.merge_calls, [(89, "newhead", "squash")])
        self.assertIn((51, "done"), gh.label_history)


class InReviewParkWatermarkTest(unittest.TestCase, _PatchedWorkflowMixin):
    """A park inside `_handle_in_review` posts an issue comment. The watermark
    must be bumped past that comment so the next tick does not see the
    orchestrator's own HITL ping as fresh PR feedback and resume the dev
    agent against it.
    """

    def _setup_failed_checks(self):
        gh = FakeGitHubClient()
        issue = make_issue(60, label="in_review")
        gh.add_issue(issue)
        pr = FakePR(
            number=70, head_branch="orchestrator/issue-60",
            head=FakePRRef(sha="cafe1234"),
            approved=True, approval_head_sha="cafe1234",
            mergeable=True, check_state="failure",
        )
        gh.add_pr(pr)
        gh.seed_state(
            60, pr_number=70, branch="orchestrator/issue-60",
            dev_agent="claude", dev_session_id="dev-sess",
            pr_last_comment_id=900,  # an old watermark from validating handoff
        )
        return gh, issue

    def test_failed_checks_park_does_not_replay_on_next_tick(self) -> None:
        gh, issue = self._setup_failed_checks()

        with patch.object(config, "AUTO_MERGE", True):
            # Tick 1: fail-checks park.
            self._run(
                lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
                run_agent=_agent(),
            )
        self.assertTrue(gh.pinned_data(60).get("awaiting_human"))
        comments_after_park = len(gh.posted_comments)
        self.assertGreater(comments_after_park, 0)
        # Watermark must have been bumped past the park comment -- which
        # means it's at or above the latest comment id on the issue.
        latest_id = gh.latest_comment_id(issue)
        self.assertEqual(gh.pinned_data(60).get("pr_last_comment_id"), latest_id)

        with patch.object(config, "AUTO_MERGE", True):
            # Tick 2: nothing new; must NOT resume the dev agent.
            mocks = self._run(
                lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
                run_agent=_agent(),
            )
        mocks["run_agent"].assert_not_called()
        # No additional comments posted (no second park, no dev-resume ping).
        self.assertEqual(len(gh.posted_comments), comments_after_park)

    def test_unmergeable_in_review_route_does_not_replay_on_next_tick(self) -> None:
        # An unmergeable PR routes to `resolving_conflict` on the first
        # in_review tick. The label change means the dispatcher hands the
        # next tick to `_handle_resolving_conflict`, not `_handle_in_review`,
        # so the in_review handler must not be re-triggered against the
        # auto-resolution-in-progress PR.
        gh = FakeGitHubClient()
        issue = make_issue(61, label="in_review")
        gh.add_issue(issue)
        pr = FakePR(
            number=71, head_branch="orchestrator/issue-61",
            head=FakePRRef(sha="cafe1234"),
            approved=True, approval_head_sha="cafe1234",
            mergeable=False, check_state="success",
        )
        gh.add_pr(pr)
        gh.seed_state(
            61, pr_number=71, branch="orchestrator/issue-61",
            dev_agent="claude", dev_session_id="dev-sess",
            pr_last_comment_id=900,
        )

        with patch.object(config, "AUTO_MERGE", True):
            self._run(
                lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
                run_agent=_agent(),
            )
        # First tick flips to resolving_conflict (no awaiting_human park).
        self.assertIn((61, "resolving_conflict"), gh.label_history)
        data = gh.pinned_data(61)
        self.assertFalse(data.get("awaiting_human"))
        self.assertEqual(data.get("conflict_round"), 0)


class InReviewSplitWatermarkTest(unittest.TestCase, _PatchedWorkflowMixin):
    """Issue comments and PR inline review comments live in different id
    namespaces in GitHub's REST API. The handler tracks them with two
    independent watermarks so a high id on one side cannot eclipse newer
    comments on the other.
    """

    BRANCH = "orchestrator/issue-65"
    PR_NUMBER = 95

    def _setup(self, *, issue_comments=(), review_comments=(), state_extra=None):
        gh = FakeGitHubClient()
        issue = make_issue(65, label="in_review")
        gh.add_issue(issue)
        pr = FakePR(
            number=self.PR_NUMBER, head_branch=self.BRANCH,
            head=FakePRRef(sha="cafe1234"),
            issue_comments=list(issue_comments),
            review_comments=list(review_comments),
        )
        gh.add_pr(pr)
        state = dict(
            pr_number=self.PR_NUMBER, branch=self.BRANCH,
            dev_agent="claude", dev_session_id="dev-sess",
        )
        if state_extra:
            state.update(state_extra)
        gh.seed_state(65, **state)
        return gh, issue, pr

    def test_inline_review_comment_triggers_resume(self) -> None:
        long_ago = datetime.now(timezone.utc) - timedelta(hours=1)
        gh, issue, pr = self._setup(
            review_comments=[
                FakeComment(
                    id=42, body="line 12: rename foo to bar",
                    user=FakeUser("alice"), created_at=long_ago,
                ),
            ],
            # Inline-review watermark just below the comment id so it
            # surfaces as fresh feedback. An unset watermark would trip the
            # legacy in_review migration and treat id=42 as already-consumed.
            state_extra={"pr_last_review_comment_id": 41},
        )

        mocks = self._run(
            lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
            run_agent=_agent(session_id="dev-sess", last_message="renamed"),
            push_branch=True,
            head_shas=["aaa", "bbb"],
        )

        self.assertEqual(mocks["run_agent"].call_count, 1)
        self.assertIn("rename foo to bar", mocks["run_agent"].call_args.args[1])
        self.assertIn((65, "validating"), gh.label_history)
        data = gh.pinned_data(65)
        self.assertEqual(data.get("pr_last_review_comment_id"), 42)
        # Issue-comment watermark stays at the legacy-migration default (0)
        # because no issue-side comment was consumed -- the two id spaces
        # ratchet independently. The migration always persists 0 instead of
        # leaving the watermark unset, so the next tick does not re-run the
        # migration past any newly-arrived first comment.
        self.assertEqual(data.get("pr_last_comment_id"), 0)

    def test_id_overlap_across_spaces_does_not_drop_comments(self) -> None:
        # Inline review comment id (5) is LOWER than the issue-comment
        # watermark (1000). With one merged-id watermark this comment would
        # be silently filtered out; with split watermarks it gets through.
        long_ago = datetime.now(timezone.utc) - timedelta(hours=1)
        gh, issue, pr = self._setup(
            review_comments=[
                FakeComment(
                    id=5, body="please add a docstring",
                    user=FakeUser("alice"), created_at=long_ago,
                ),
            ],
            # Issue-side watermark high (1000), inline-review watermark low (4)
            # -- the two ratchet independently, and id=5 must still surface.
            state_extra={
                "pr_last_comment_id": 1000,
                "pr_last_review_comment_id": 4,
            },
        )

        mocks = self._run(
            lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
            run_agent=_agent(session_id="dev-sess", last_message="added"),
            push_branch=True,
            head_shas=["aaa", "bbb"],
        )

        # The inline comment is consumed even though id=5 < pr_last_comment_id=1000.
        self.assertEqual(mocks["run_agent"].call_count, 1)
        self.assertIn("please add a docstring", mocks["run_agent"].call_args.args[1])
        self.assertEqual(gh.pinned_data(65).get("pr_last_review_comment_id"), 5)


class HumanChangesRequestedVetoTest(unittest.TestCase, _PatchedWorkflowMixin):
    """A human CHANGES_REQUESTED review on the PR's current head must veto
    auto-merge regardless of how the reviewer agent voted. Without the veto,
    the `agent_approved_sha == head_sha` short-circuit would let the
    orchestrator merge over a standing human objection on the same SHA.
    """

    def test_changes_requested_blocks_auto_merge_even_when_agent_approved(self) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(80, label="in_review")
        gh.add_issue(issue)
        pr = FakePR(
            number=120, head_branch="orchestrator/issue-80",
            head=FakePRRef(sha="cafe1234"),
            mergeable=True, check_state="success",
            changes_requested=True,  # human vetoed the current head
        )
        gh.add_pr(pr)
        gh.seed_state(
            80, pr_number=120, branch="orchestrator/issue-80",
            agent_approved_sha="cafe1234",  # agent approved same head
        )

        with patch.object(config, "AUTO_MERGE", True):
            self._run(
                lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
                run_agent=_agent(),
            )

        # Veto wins over agent approval; no merge, no label flip.
        self.assertEqual(gh.merge_calls, [])
        self.assertEqual(gh.label_history, [])
        self.assertFalse(gh.pinned_data(80).get("awaiting_human"))

    def test_changes_requested_blocks_auto_merge_even_with_human_approval(self) -> None:
        # APPROVED + CHANGES_REQUESTED on the same head: GitHub considers
        # the PR not approved. pr_is_approved already filters this out, but
        # the orthogonal veto check is what guarantees the agent path can't
        # bypass it via agent_approved_sha.
        gh = FakeGitHubClient()
        issue = make_issue(81, label="in_review")
        gh.add_issue(issue)
        pr = FakePR(
            number=121, head_branch="orchestrator/issue-81",
            head=FakePRRef(sha="cafe1234"),
            mergeable=True, check_state="success",
            approved=True, approval_head_sha="cafe1234",
            changes_requested=True,
        )
        gh.add_pr(pr)
        gh.seed_state(
            81, pr_number=121, branch="orchestrator/issue-81",
            agent_approved_sha="cafe1234",
        )

        with patch.object(config, "AUTO_MERGE", True):
            self._run(
                lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
                run_agent=_agent(),
            )

        self.assertEqual(gh.merge_calls, [])
        self.assertEqual(gh.label_history, [])

    def test_stale_changes_requested_does_not_block(self) -> None:
        # CHANGES_REQUESTED on an OLD head (force-pushed past) must not
        # block auto-merge: a stale veto on a no-longer-current SHA is
        # equivalent to no veto. Mirrors the stale-approval gating.
        gh = FakeGitHubClient()
        issue = make_issue(82, label="in_review")
        gh.add_issue(issue)
        pr = FakePR(
            number=122, head_branch="orchestrator/issue-82",
            head=FakePRRef(sha="newhead"),
            mergeable=True, check_state="success",
            changes_requested=True, changes_requested_head_sha="oldhead",
        )
        gh.add_pr(pr)
        gh.seed_state(
            82, pr_number=122, branch="orchestrator/issue-82",
            agent_approved_sha="newhead",
        )

        with patch.object(config, "AUTO_MERGE", True):
            self._run(
                lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
                run_agent=_agent(),
            )

        self.assertEqual(gh.merge_calls, [(122, "newhead", "squash")])
        self.assertIn((82, "done"), gh.label_history)


class InReviewPRReviewSummaryTest(unittest.TestCase, _PatchedWorkflowMixin):
    """A human can leave PR feedback either through inline review comments
    or through the *review summary* body (the textbox above the
    Approve / Request Changes / Comment buttons). The summary lives in the
    PullRequestReview id namespace, distinct from issue comments and inline
    review comments. Without surfacing it, a "Comment" review with body is
    silently auto-merged over and a CHANGES_REQUESTED summary blocks merge
    without the dev ever seeing the feedback.
    """

    PR_NUMBER = 130
    BRANCH = "orchestrator/issue-90"

    def _setup_with_review(self, review):
        gh = FakeGitHubClient()
        issue = make_issue(90, label="in_review")
        gh.add_issue(issue)
        pr = FakePR(
            number=self.PR_NUMBER, head_branch=self.BRANCH,
            head=FakePRRef(sha="cafe1234"),
            mergeable=True, check_state="success",
            reviews=[review],
        )
        gh.add_pr(pr)
        gh.seed_state(
            90, pr_number=self.PR_NUMBER, branch=self.BRANCH,
            dev_agent="claude", dev_session_id="dev-sess",
            agent_approved_sha="cafe1234",
            # Watermarks below the seeded review id so the body surfaces as
            # fresh feedback. An unset summary watermark would trip the
            # legacy in_review migration and mask the review.
            pr_last_comment_id=999,
            pr_last_review_summary_id=0,
        )
        return gh, issue, pr

    def test_changes_requested_with_body_resumes_dev(self) -> None:
        long_ago = datetime.now(timezone.utc) - timedelta(hours=1)
        review = FakePRReview(
            id=4242,
            body="please rename foo to bar in the public API",
            state="CHANGES_REQUESTED",
            user=FakeUser("alice"),
            submitted_at=long_ago,
            commit_id="cafe1234",
        )
        gh, issue, pr = self._setup_with_review(review)

        with patch.object(config, "AUTO_MERGE", True), \
             patch.object(config, "IN_REVIEW_DEBOUNCE_SECONDS", 600):
            mocks = self._run(
                lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
                run_agent=_agent(
                    session_id="dev-sess", last_message="renamed",
                ),
                push_branch=True,
                head_shas=["aaa", "bbb"],
            )

        # Dev resumed with the review body quoted into the prompt; pushed;
        # bounced to validating; summary watermark advanced past the review.
        self.assertEqual(mocks["run_agent"].call_count, 1)
        self.assertIn(
            "rename foo to bar",
            mocks["run_agent"].call_args.args[1],
        )
        self.assertIn((90, "validating"), gh.label_history)
        self.assertEqual(gh.merge_calls, [])
        data = gh.pinned_data(90)
        self.assertEqual(data.get("pr_last_review_summary_id"), 4242)
        self.assertEqual(data.get("review_round"), 0)

    def test_commented_review_with_body_resumes_dev(self) -> None:
        # A "Comment" review (state=COMMENTED) doesn't block via
        # pr_has_changes_requested, so without surfacing the body the
        # auto-merge gate would proceed and merge over the human's note.
        long_ago = datetime.now(timezone.utc) - timedelta(hours=1)
        review = FakePRReview(
            id=4243,
            body="how about adding a smoke test for the empty-input case?",
            state="COMMENTED",
            user=FakeUser("alice"),
            submitted_at=long_ago,
        )
        gh, issue, pr = self._setup_with_review(review)

        with patch.object(config, "AUTO_MERGE", True), \
             patch.object(config, "IN_REVIEW_DEBOUNCE_SECONDS", 600):
            mocks = self._run(
                lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
                run_agent=_agent(
                    session_id="dev-sess", last_message="added test",
                ),
                push_branch=True,
                head_shas=["aaa", "bbb"],
            )

        self.assertEqual(mocks["run_agent"].call_count, 1)
        self.assertIn(
            "smoke test for the empty-input case",
            mocks["run_agent"].call_args.args[1],
        )
        # Auto-merge did NOT fire over the human's comment.
        self.assertEqual(gh.merge_calls, [])
        self.assertIn((90, "validating"), gh.label_history)

    def test_approved_review_body_does_not_trigger_resume(self) -> None:
        # APPROVED reviews are excluded from the summary surface even when
        # they carry an informational body. The human approved the PR --
        # their note is not a request for changes.
        long_ago = datetime.now(timezone.utc) - timedelta(hours=1)
        review = FakePRReview(
            id=4244, body="LGTM, ship it", state="APPROVED",
            user=FakeUser("alice"), submitted_at=long_ago,
        )
        gh, issue, pr = self._setup_with_review(review)
        # APPROVED on the live head also satisfies the auto-merge gate
        # via pr_is_approved.
        pr.approved = True
        pr.approval_head_sha = "cafe1234"

        with patch.object(config, "AUTO_MERGE", True), \
             patch.object(config, "IN_REVIEW_DEBOUNCE_SECONDS", 600):
            mocks = self._run(
                lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
                run_agent=_agent(),
            )

        mocks["run_agent"].assert_not_called()
        # Auto-merge proceeds; the summary surface ignored the APPROVED body.
        self.assertEqual(
            gh.merge_calls, [(self.PR_NUMBER, "cafe1234", "squash")]
        )
        self.assertIn((90, "done"), gh.label_history)

    def test_empty_body_review_is_ignored(self) -> None:
        # A CHANGES_REQUESTED review with no body has nothing to forward to
        # the dev. pr_has_changes_requested still vetoes auto-merge (correct),
        # but no follow-up prompt is generated.
        long_ago = datetime.now(timezone.utc) - timedelta(hours=1)
        review = FakePRReview(
            id=4245, body="", state="CHANGES_REQUESTED",
            user=FakeUser("alice"), submitted_at=long_ago,
        )
        gh, issue, pr = self._setup_with_review(review)
        # Mirror the pr_has_changes_requested veto path.
        pr.changes_requested = True
        pr.changes_requested_head_sha = "cafe1234"

        with patch.object(config, "AUTO_MERGE", True), \
             patch.object(config, "IN_REVIEW_DEBOUNCE_SECONDS", 600):
            mocks = self._run(
                lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
                run_agent=_agent(),
            )

        mocks["run_agent"].assert_not_called()
        # Veto blocked the merge; no label flip.
        self.assertEqual(gh.merge_calls, [])
        self.assertEqual(gh.label_history, [])


class SameAccountHumanFeedbackTest(unittest.TestCase, _PatchedWorkflowMixin):
    """Operators commonly run the orchestrator with a personal PAT and also
    review PRs by hand from that same GitHub account. The self-comment filter
    must not key on author login -- if it did, real human review feedback from
    that account would be dropped as bot noise and AUTO_MERGE could land a
    'please do not merge' comment.

    The fix tracks orchestrator-authored comments by exact id (recorded when
    the orchestrator posts them via `_post_issue_comment` /
    `_post_pr_comment`). A human comment from the PAT login carries an id the
    orchestrator never recorded, so it surfaces as fresh PR feedback and the
    auto-merge gate stays closed.
    """

    PR_NUMBER = 200
    BRANCH = "orchestrator/issue-100"

    def test_same_account_human_pr_comment_blocks_auto_merge(self) -> None:
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
            agent_approved_sha="cafe1234",
            # Watermark just past the orchestrator's earlier comments and the
            # human's id-3000 comment. Filter must drop only ids the
            # orchestrator actually recorded.
            pr_last_comment_id=2999,
            orchestrator_comment_ids=[900, 901],
            pickup_comment_id=900,
        )

        with patch.object(config, "AUTO_MERGE", True), \
             patch.object(config, "IN_REVIEW_DEBOUNCE_SECONDS", 600):
            mocks = self._run(
                lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
                run_agent=_agent(
                    session_id="dev-sess", last_message="standing by"
                ),
                push_branch=True,
                head_shas=["aaa", "bbb"],
            )

        # Auto-merge must not fire over the human's standing objection.
        self.assertEqual(gh.merge_calls, [])
        self.assertNotIn((100, "done"), gh.label_history)
        # The human comment is treated as fresh feedback: the dev session
        # is resumed on it and the issue bounces back to validating.
        self.assertEqual(mocks["run_agent"].call_count, 1)
        self.assertIn(
            "please do not merge yet",
            mocks["run_agent"].call_args.args[1],
        )
        self.assertIn((100, "validating"), gh.label_history)

    def test_same_account_human_issue_comment_at_handoff_is_preserved(self) -> None:
        # Validating-handoff variant: a human posts a review comment on the
        # issue thread (under the same account that owns the PAT) while
        # validating is still running. Without the id-based filter, the
        # handoff would advance the watermark past the human comment as if
        # it were the orchestrator's own self-run, then auto-merge over it.
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
        # and the dev gets resumed -- not auto-merged.
        if not any(l.name == "in_review" for l in issue.labels):
            issue.labels = [FakeLabel("in_review")]
        with patch.object(config, "AUTO_MERGE", True), \
             patch.object(config, "IN_REVIEW_DEBOUNCE_SECONDS", 600):
            mocks = self._run(
                lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
                run_agent=_agent(
                    session_id="dev-sess", last_message="docstring added"
                ),
                push_branch=True,
                head_shas=["aaa", "bbb"],
            )

        self.assertEqual(mocks["run_agent"].call_count, 1)
        self.assertIn(
            "please add a docstring",
            mocks["run_agent"].call_args.args[1],
        )
        self.assertEqual(gh.merge_calls, [])
        self.assertIn((101, "validating"), gh.label_history)


class LegacyInReviewWatermarkSeedTest(unittest.TestCase, _PatchedWorkflowMixin):
    """An issue that reached `in_review` before validating started seeding
    watermarks (or that was manually relabeled, or whose handoff failed to
    snapshot the PR) sits on the in_review handler with all three watermarks
    unset. Without the first-tick migration, every historical comment --
    including the orchestrator's own pickup / PR-opened / approval messages
    -- would surface as fresh PR feedback once the debounce expired,
    resuming the dev and bouncing the PR back to validating.
    """

    PR_NUMBER = 300
    BRANCH = "orchestrator/issue-150"

    def _legacy_setup(self):
        gh = FakeGitHubClient()
        long_ago = datetime.now(timezone.utc) - timedelta(hours=1)
        # Three historical orchestrator comments on the issue thread plus
        # one historical PR conversation comment (the validating handoff
        # approval) -- exactly the shape of an in-flight in_review issue
        # whose state was written before pr_last_comment_id existed.
        issue = make_issue(150, label="in_review", comments=[
            FakeComment(
                id=910, body=":robot: orchestrator picking this up.",
                user=FakeUser("orchestrator"), created_at=long_ago,
            ),
            FakeComment(
                id=911, body=":sparkles: PR opened: #300",
                user=FakeUser("orchestrator"), created_at=long_ago,
            ),
        ])
        gh.add_issue(issue)
        pr = FakePR(
            number=self.PR_NUMBER, head_branch=self.BRANCH,
            head=FakePRRef(sha="cafe1234"),
            mergeable=True, check_state="success",
            issue_comments=[
                FakeComment(
                    id=920,
                    body=":white_check_mark: codex review approved.",
                    user=FakeUser("orchestrator"),
                    created_at=long_ago,
                ),
            ],
            review_comments=[
                FakeComment(
                    id=30, body="line 5: drop the trailing newline",
                    user=FakeUser("alice"), created_at=long_ago,
                ),
            ],
            reviews=[
                FakePRReview(
                    id=4000, body="please rename foo to bar",
                    state="CHANGES_REQUESTED",
                    user=FakeUser("alice"),
                    submitted_at=long_ago,
                    commit_id="cafe1234",
                ),
            ],
        )
        gh.add_pr(pr)
        # Legacy state: pr_number is set, but no watermarks AND no recorded
        # orchestrator_comment_ids. This is the state shape the migration
        # has to handle without replaying every historical comment.
        gh.seed_state(
            150, pr_number=self.PR_NUMBER, branch=self.BRANCH,
            dev_agent="claude", dev_session_id="dev-sess",
            agent_approved_sha="cafe1234",
        )
        return gh, issue, pr

    def test_legacy_first_tick_does_not_replay_history(self) -> None:
        gh, issue, pr = self._legacy_setup()

        with patch.object(config, "AUTO_MERGE", False), \
             patch.object(config, "IN_REVIEW_DEBOUNCE_SECONDS", 600):
            mocks = self._run(
                lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
                run_agent=_agent(),
            )

        # No dev resume despite historical comments / inline review / review
        # summary all sitting visible: the migration seeded each watermark
        # past the latest visible id on its surface.
        mocks["run_agent"].assert_not_called()
        self.assertNotIn((150, "validating"), gh.label_history)
        # Watermarks were persisted so subsequent ticks see only newer ids.
        data = gh.pinned_data(150)
        self.assertGreaterEqual(data.get("pr_last_comment_id"), 920)
        self.assertEqual(data.get("pr_last_review_comment_id"), 30)
        self.assertEqual(data.get("pr_last_review_summary_id"), 4000)

    def test_legacy_first_tick_does_not_block_auto_merge(self) -> None:
        # AUTO_MERGE on with all gates passing: the migration must not park
        # or otherwise block the merge -- it only treats already-visible
        # comments as consumed.
        gh, issue, pr = self._legacy_setup()
        # Drop the historical review-summary so pr_has_changes_requested
        # doesn't veto via a separate path; the migration should still seed
        # the summary watermark past the inline review and then merge.
        pr.reviews = []

        with patch.object(config, "AUTO_MERGE", True), \
             patch.object(config, "IN_REVIEW_DEBOUNCE_SECONDS", 600):
            self._run(
                lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
                run_agent=_agent(),
            )

        self.assertEqual(
            gh.merge_calls, [(self.PR_NUMBER, "cafe1234", "squash")]
        )
        self.assertIn((150, "done"), gh.label_history)


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
            agent_approved_sha="cafe1234",
            pr_last_comment_id=4242,
            pr_last_review_comment_id=4241,
            pr_last_review_summary_id=0,
            orchestrator_comment_ids=[4242],
        )

        with patch.object(config, "AUTO_MERGE", True), \
             patch.object(config, "IN_REVIEW_DEBOUNCE_SECONDS", 600):
            mocks = self._run(
                lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
                run_agent=_agent(
                    session_id="dev-sess", last_message="renamed",
                ),
                push_branch=True,
                head_shas=["aaa", "bbb"],
            )

        # Inline review comment id=4242 surfaces despite colliding with the
        # recorded IssueComment id 4242; auto-merge does not fire.
        self.assertEqual(mocks["run_agent"].call_count, 1)
        self.assertIn(
            "rename foo to bar",
            mocks["run_agent"].call_args.args[1],
        )
        self.assertEqual(gh.merge_calls, [])
        self.assertIn((160, "validating"), gh.label_history)

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
            agent_approved_sha="cafe1234",
            pr_last_comment_id=5000,
            pr_last_review_comment_id=0,
            pr_last_review_summary_id=4999,
            orchestrator_comment_ids=[5000],
        )

        with patch.object(config, "AUTO_MERGE", True), \
             patch.object(config, "IN_REVIEW_DEBOUNCE_SECONDS", 600):
            mocks = self._run(
                lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
                run_agent=_agent(
                    session_id="dev-sess", last_message="tightened",
                ),
                push_branch=True,
                head_shas=["aaa", "bbb"],
            )

        self.assertEqual(mocks["run_agent"].call_count, 1)
        self.assertIn(
            "tighten the spec",
            mocks["run_agent"].call_args.args[1],
        )
        self.assertEqual(gh.merge_calls, [])
        self.assertIn((161, "validating"), gh.label_history)


class TransientParkRecoveryTest(unittest.TestCase, _PatchedWorkflowMixin):
    """An auto-merge candidate that parked on failed checks or unmergeability
    must auto-recover when the underlying GitHub state changes silently
    (CI rerun goes green, rebase resolves a conflict). Otherwise a human
    who fixes the transient condition without leaving a comment leaves the
    issue stuck in_review forever.
    """

    PR_NUMBER = 500
    BRANCH = "orchestrator/issue-170"

    def _parked_issue(self, *, park_reason: str, pr_kwargs: dict):
        gh = FakeGitHubClient()
        issue = make_issue(170, label="in_review")
        gh.add_issue(issue)
        pr = FakePR(
            number=self.PR_NUMBER, head_branch=self.BRANCH,
            head=FakePRRef(sha="cafe1234"),
            **pr_kwargs,
        )
        gh.add_pr(pr)
        gh.seed_state(
            170, pr_number=self.PR_NUMBER, branch=self.BRANCH,
            dev_agent="claude", dev_session_id="dev-sess",
            agent_approved_sha="cafe1234",
            awaiting_human=True,
            park_reason=park_reason,
            # Watermarks past everything visible -- mirrors what
            # _bump_in_review_watermarks set when the original park ran.
            pr_last_comment_id=10_000,
            pr_last_review_comment_id=10_000,
            pr_last_review_summary_id=10_000,
        )
        return gh, issue, pr

    def test_failed_checks_park_recovers_when_checks_go_green(self) -> None:
        gh, issue, pr = self._parked_issue(
            park_reason="failed_checks",
            pr_kwargs=dict(mergeable=True, check_state="success"),
        )

        with patch.object(config, "AUTO_MERGE", True):
            self._run(
                lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
                run_agent=_agent(),
            )

        self.assertEqual(
            gh.merge_calls, [(self.PR_NUMBER, "cafe1234", "squash")]
        )
        self.assertIn((170, "done"), gh.label_history)
        # Park flags cleared so subsequent ticks proceed normally.
        data = gh.pinned_data(170)
        self.assertFalse(data.get("awaiting_human"))
        self.assertIsNone(data.get("park_reason"))

    def test_unmergeable_park_recovers_when_pr_becomes_mergeable(self) -> None:
        gh, issue, pr = self._parked_issue(
            park_reason="unmergeable",
            pr_kwargs=dict(mergeable=True, check_state="success"),
        )

        with patch.object(config, "AUTO_MERGE", True):
            self._run(
                lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
                run_agent=_agent(),
            )

        self.assertEqual(
            gh.merge_calls, [(self.PR_NUMBER, "cafe1234", "squash")]
        )
        self.assertIn((170, "done"), gh.label_history)

    def test_failed_checks_park_stays_parked_when_checks_still_failing(
        self,
    ) -> None:
        # Recovery must not re-post the park message when the gate still
        # fails -- otherwise every poll would spam the issue.
        gh, issue, pr = self._parked_issue(
            park_reason="failed_checks",
            pr_kwargs=dict(mergeable=True, check_state="failure"),
        )

        with patch.object(config, "AUTO_MERGE", True):
            self._run(
                lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
                run_agent=_agent(),
            )

        self.assertEqual(gh.merge_calls, [])
        self.assertEqual(gh.label_history, [])
        # No new park comment posted on this tick.
        self.assertEqual(gh.posted_comments, [])
        # Park flags preserved for the next recovery attempt.
        data = gh.pinned_data(170)
        self.assertTrue(data.get("awaiting_human"))
        self.assertEqual(data.get("park_reason"), "failed_checks")

    def test_non_transient_park_stays_parked_even_when_gates_pass(self) -> None:
        # A park whose reason is not in the transient set (e.g. a missing
        # pr_number, a dev-fix failure) needs explicit human action and must
        # not recover from gate state alone.
        gh, issue, pr = self._parked_issue(
            park_reason="dev_fix_failed",
            pr_kwargs=dict(mergeable=True, check_state="success"),
        )

        with patch.object(config, "AUTO_MERGE", True):
            self._run(
                lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
                run_agent=_agent(),
            )

        self.assertEqual(gh.merge_calls, [])
        self.assertEqual(gh.label_history, [])


class ManuallyClosedInReviewIssueTest(unittest.TestCase, _PatchedWorkflowMixin):
    """An open in_review issue closed manually by a human is a stop signal.
    The closed-in_review sweep yields the issue (so a Resolves-#N auto-close
    can finalize to `done`), but if the linked PR is still open the sweep
    has surfaced a manually-closed issue and `_handle_in_review` must mark
    it rejected before the auto-merge gates can run -- otherwise AUTO_MERGE
    can land the PR over the human's rejection.
    """

    PR_NUMBER = 700
    BRANCH = "orchestrator/issue-250"

    def _setup(self, **pr_kwargs):
        gh = FakeGitHubClient()
        issue = make_issue(250, label="in_review")
        issue.closed = True  # human closed the issue, PR still open
        gh.add_issue(issue)
        defaults = dict(
            number=self.PR_NUMBER, head_branch=self.BRANCH,
            head=FakePRRef(sha="cafe1234"),
            mergeable=True, check_state="success",
        )
        defaults.update(pr_kwargs)
        pr = FakePR(**defaults)
        gh.add_pr(pr)
        gh.seed_state(
            250, pr_number=self.PR_NUMBER, branch=self.BRANCH,
            dev_agent="claude", dev_session_id="dev-sess",
            agent_approved_sha="cafe1234",
            pr_last_comment_id=999,
            pr_last_review_comment_id=0,
            pr_last_review_summary_id=0,
        )
        return gh, issue, pr

    def test_manually_closed_with_open_pr_marks_rejected(self) -> None:
        gh, issue, pr = self._setup()

        with patch.object(config, "AUTO_MERGE", True):
            mocks = self._run(
                lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
                run_agent=_agent(),
            )

        # AUTO_MERGE must not fire over a manually-closed issue even though
        # every gate (approval, mergeable, success) would otherwise pass.
        self.assertEqual(gh.merge_calls, [])
        self.assertIn((250, "rejected"), gh.label_history)
        self.assertNotIn((250, "done"), gh.label_history)
        self.assertIn("closed_without_merge_at", gh.pinned_data(250))
        # Closing the issue while the PR is still open is a human stop
        # signal. The PR may still be useful for inspection / salvage, so
        # cleanup must NOT delete the branch here -- the operator drives
        # that, or it fires once the PR itself is closed.
        mocks["_cleanup_terminal_branch"].assert_not_called()

    def test_manually_closed_then_pr_closed_later_requires_manual_cleanup(
        self,
    ) -> None:
        # Documents the known caveat: once the orchestrator flips the
        # closed-issue to `rejected`, the issue falls outside the
        # closed-issue sweep (`list_pollable_issues` only sweeps closed
        # issues still labeled `in_review` / `resolving_conflict`) AND
        # the dispatcher is a no-op for `rejected`. A subsequent PR close
        # is therefore never observed by the orchestrator and the
        # operator must clean up the branch / worktree by hand.
        gh, issue, pr = self._setup()
        with patch.object(config, "AUTO_MERGE", False):
            mocks = self._run(
                lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
                run_agent=_agent(),
            )
        self.assertIn((250, "rejected"), gh.label_history)
        mocks["_cleanup_terminal_branch"].assert_not_called()

        # Operator now closes the PR. The issue is already closed +
        # rejected, so the polling sweep does not include it on the next
        # tick -- the handler never runs and cleanup never fires.
        pr.state = "closed"
        pollable_numbers = {i.number for i in gh.list_pollable_issues()}
        self.assertNotIn(
            250, pollable_numbers,
            "rejected closed issues are not swept, so the orchestrator "
            "cannot observe the later PR close; cleanup must be manual.",
        )

    def test_manually_closed_does_not_resume_dev_on_new_comments(self) -> None:
        # Even with new PR feedback past the watermark, a manually-closed
        # issue should not spawn a dev fix -- the human closing the issue
        # superseded any open feedback.
        long_ago = datetime.now(timezone.utc) - timedelta(hours=1)
        gh, issue, pr = self._setup()
        pr.issue_comments.append(
            FakeComment(
                id=2000, body="actually let's reconsider",
                user=FakeUser("alice"), created_at=long_ago,
            ),
        )

        with patch.object(config, "AUTO_MERGE", False), \
             patch.object(config, "IN_REVIEW_DEBOUNCE_SECONDS", 600):
            mocks = self._run(
                lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
                run_agent=_agent(),
            )

        mocks["run_agent"].assert_not_called()
        self.assertIn((250, "rejected"), gh.label_history)

    def test_external_merge_with_closed_issue_still_finalizes_done(self) -> None:
        # The original closed-issue sweep purpose: a Resolves #N footer
        # auto-closes the issue when the PR merges. Issue closed AND PR
        # merged must still flip to `done`, not `rejected`.
        gh = FakeGitHubClient()
        issue = make_issue(251, label="in_review")
        issue.closed = True
        gh.add_issue(issue)
        pr = FakePR(
            number=701, head_branch="orchestrator/issue-251",
            head=FakePRRef(sha="cafe1234"),
            merged=True, state="closed",
        )
        gh.add_pr(pr)
        gh.seed_state(251, pr_number=701, branch="orchestrator/issue-251")

        self._run(
            lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
            run_agent=_agent(),
        )

        self.assertIn((251, "done"), gh.label_history)
        self.assertNotIn((251, "rejected"), gh.label_history)
        self.assertIn("merged_at", gh.pinned_data(251))


class LegacyMigrationPersistsEmptyWatermarksTest(
    unittest.TestCase, _PatchedWorkflowMixin
):
    """The legacy in_review migration runs on every tick where any of the
    three watermarks is unset. If the surface has no content yet, the
    migration would previously leave the watermark unset and re-fire next
    tick -- the FIRST human inline / summary review added in between would
    then be consumed by the migration before _handle_in_review built
    new_comments, allowing AUTO_MERGE to land the PR over that first
    review. The migration must persist 0 even on empty surfaces so the
    next tick scans new comments instead of re-migrating.
    """

    PR_NUMBER = 900
    BRANCH = "orchestrator/issue-400"

    def _legacy_setup(self):
        gh = FakeGitHubClient()
        # Make 'truly legacy': no watermarks at all on any surface, no
        # comments anywhere. This is the shape the reviewer flagged --
        # snapshot-failed handoff or pre-feature in_review state with an
        # empty PR.
        issue = make_issue(400, label="in_review")
        gh.add_issue(issue)
        pr = FakePR(
            number=self.PR_NUMBER, head_branch=self.BRANCH,
            head=FakePRRef(sha="cafe1234"),
            mergeable=True, check_state="success",
        )
        gh.add_pr(pr)
        gh.seed_state(
            400, pr_number=self.PR_NUMBER, branch=self.BRANCH,
            dev_agent="claude", dev_session_id="dev-sess",
        )
        return gh, issue, pr

    def test_first_inline_review_after_migration_surfaces(self) -> None:
        gh, issue, pr = self._legacy_setup()

        # Tick 1: legacy migration runs, surfaces have nothing to seed past.
        # The migration must persist 0 on every namespace anyway.
        with patch.object(config, "AUTO_MERGE", False):
            self._run(
                lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
                run_agent=_agent(),
            )
        data = gh.pinned_data(400)
        self.assertEqual(data.get("pr_last_review_comment_id"), 0)
        self.assertEqual(data.get("pr_last_review_summary_id"), 0)
        self.assertEqual(data.get("pr_last_comment_id"), 0)

        # Now a human posts the first inline review comment. With the fix,
        # the next tick sees pr_last_review_comment_id=0 (already set) and
        # surfaces id=42 instead of re-running migration past it.
        long_ago = datetime.now(timezone.utc) - timedelta(hours=1)
        pr.review_comments.append(
            FakeComment(
                id=42, body="line 7: rename foo to bar",
                user=FakeUser("alice"), created_at=long_ago,
            ),
        )

        with patch.object(config, "AUTO_MERGE", True), \
             patch.object(config, "IN_REVIEW_DEBOUNCE_SECONDS", 600):
            mocks = self._run(
                lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
                run_agent=_agent(
                    session_id="dev-sess", last_message="renamed",
                ),
                push_branch=True,
                head_shas=["aaa", "bbb"],
            )

        # The first inline review comment after migration is treated as
        # fresh feedback and resumes the dev.
        self.assertEqual(mocks["run_agent"].call_count, 1)
        self.assertIn(
            "rename foo to bar",
            mocks["run_agent"].call_args.args[1],
        )
        self.assertEqual(gh.merge_calls, [])
        self.assertIn((400, "validating"), gh.label_history)

    def test_first_review_summary_after_migration_surfaces(self) -> None:
        # Same shape on the review-summary surface. A COMMENTED summary
        # body is the dangerous case here: pr_has_changes_requested does
        # not veto and AUTO_MERGE could otherwise land the PR over it.
        gh, issue, pr = self._legacy_setup()
        # Need agent_approved_sha so the auto-merge path doesn't bail on
        # missing approval -- mirrors a freshly-handed-off issue.
        gh.seed_state(
            400, pr_number=self.PR_NUMBER, branch=self.BRANCH,
            dev_agent="claude", dev_session_id="dev-sess",
            agent_approved_sha="cafe1234",
        )

        with patch.object(config, "AUTO_MERGE", False):
            self._run(
                lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
                run_agent=_agent(),
            )
        data = gh.pinned_data(400)
        self.assertEqual(data.get("pr_last_review_summary_id"), 0)

        long_ago = datetime.now(timezone.utc) - timedelta(hours=1)
        pr.reviews.append(
            FakePRReview(
                id=5050, body="please tighten the spec",
                state="COMMENTED",
                user=FakeUser("alice"),
                submitted_at=long_ago,
                commit_id="cafe1234",
            ),
        )

        with patch.object(config, "AUTO_MERGE", True), \
             patch.object(config, "IN_REVIEW_DEBOUNCE_SECONDS", 600):
            mocks = self._run(
                lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
                run_agent=_agent(
                    session_id="dev-sess", last_message="tightened",
                ),
                push_branch=True,
                head_shas=["aaa", "bbb"],
            )

        self.assertEqual(mocks["run_agent"].call_count, 1)
        self.assertIn(
            "tighten the spec",
            mocks["run_agent"].call_args.args[1],
        )
        self.assertEqual(gh.merge_calls, [])
        self.assertIn((400, "validating"), gh.label_history)


class GitHubClientClosedIssueSweepLabelTest(unittest.TestCase):
    """Real PyGithub's `Repository.get_issues(labels=...)` expects Label
    OBJECTS and reads `label.name`. The closed-issue sweep used to pass a
    raw string list, which raises a TypeError before the generator yields
    anything; because that exception escapes the per-issue try/except in
    `tick()`, every tick after open issues are processed would fail and
    externally-merged in_review issues would never finalize to `done`.

    This test pokes the real `GitHubClient.list_pollable_issues` against a
    mocked Repository to verify the call passes a Label object.
    """

    def test_closed_sweep_uses_label_object_from_get_label(self) -> None:
        from unittest.mock import MagicMock
        from orchestrator.github import GitHubClient

        # Bypass __init__: it would require a real PAT and Github client.
        client = GitHubClient.__new__(GitHubClient)
        client.repo = MagicMock()
        # All get_issues calls (open sweep + per-label closed sweeps)
        # return nothing -- we only care about the call arguments.
        client.repo.get_issues.return_value = iter([])
        in_review_label = MagicMock(name="in_review_label")
        resolving_label = MagicMock(name="resolving_conflict_label")

        def fake_get_label(name: str):
            return {
                "in_review": in_review_label,
                "resolving_conflict": resolving_label,
            }[name]

        client.repo.get_label.side_effect = fake_get_label

        list(client.list_pollable_issues())

        # Both labels are looked up by name (one query per label because
        # the GitHub Issues API treats `labels` as AND, not OR -- a single
        # query for "either label" is impossible).
        looked_up = [
            ca.args[0] for ca in client.repo.get_label.call_args_list
        ]
        self.assertIn("in_review", looked_up)
        self.assertIn("resolving_conflict", looked_up)
        # The closed sweeps were invoked with Label OBJECTS, not strings.
        closed_calls = [
            ca for ca in client.repo.get_issues.call_args_list
            if ca.kwargs.get("state") == "closed"
        ]
        self.assertEqual(len(closed_calls), 2)
        labels_passed = [ca.kwargs["labels"] for ca in closed_calls]
        self.assertIn([in_review_label], labels_passed)
        self.assertIn([resolving_label], labels_passed)

    def test_missing_label_skips_closed_sweep_without_raising(self) -> None:
        # If `get_label` raises (under-scoped PAT, label not yet bootstrapped)
        # the generator must complete the open-issue sweep AND swallow the
        # closed-issue branch -- otherwise `tick()` aborts mid-loop.
        from unittest.mock import MagicMock
        from orchestrator.github import GitHubClient
        from github import GithubException

        client = GitHubClient.__new__(GitHubClient)
        client.repo = MagicMock()
        client.repo.get_issues.return_value = iter([])
        client.repo.get_label.side_effect = GithubException(
            404, {"message": "Not Found"}, None
        )

        # Must not raise.
        out = list(client.list_pollable_issues())

        self.assertEqual(out, [])
        # Only the open sweep was invoked.
        states = [
            ca.kwargs.get("state")
            for ca in client.repo.get_issues.call_args_list
        ]
        self.assertEqual(states, ["open"])


class ZeroWatermarkSurvivesFallbackTest(unittest.TestCase, _PatchedWorkflowMixin):
    """A legacy validating handoff stores `pr_last_comment_id = 0` to mean
    "scan all from the beginning". The in_review fallback to
    `last_action_comment_id` must not discard 0 in favor of a higher prior
    park-comment id; otherwise lower-id human feedback (e.g. an implementing-
    time "do not merge yet") sits below the watermark and AUTO_MERGE can
    land the PR over it.
    """

    PR_NUMBER = 1100
    BRANCH = "orchestrator/issue-600"

    def test_zero_watermark_does_not_fall_back_to_last_action(self) -> None:
        gh = FakeGitHubClient()
        long_ago = datetime.now(timezone.utc) - timedelta(hours=1)
        # The implementing-time park comment (id 920) sits between a human
        # "do not merge yet" comment (id 910) and the validating-handoff
        # state. last_action_comment_id was set to 920 by the prior park.
        # If the in_review handler falls back to that for the watermark,
        # comment 910 is below it and gets dropped.
        issue = make_issue(600, label="in_review", comments=[
            FakeComment(
                id=910, body="please do not merge yet",
                user=FakeUser("alice"), created_at=long_ago,
            ),
            FakeComment(
                id=920, body=":robot: park message from a prior tick",
                user=FakeUser("orchestrator"), created_at=long_ago,
            ),
        ])
        gh.add_issue(issue)
        pr = FakePR(
            number=self.PR_NUMBER, head_branch=self.BRANCH,
            head=FakePRRef(sha="cafe1234"),
            mergeable=True, check_state="success",
        )
        gh.add_pr(pr)
        gh.seed_state(
            600,
            pr_number=self.PR_NUMBER, branch=self.BRANCH,
            dev_agent="claude", dev_session_id="dev-sess",
            agent_approved_sha="cafe1234",
            # Legacy default: 0 means "scan everything".
            pr_last_comment_id=0,
            pr_last_review_comment_id=0,
            pr_last_review_summary_id=0,
            # ALSO populated from the prior park; must NOT take precedence
            # over the legacy 0 watermark.
            last_action_comment_id=920,
            # Park the bot's own message id so the id-set filter drops it.
            orchestrator_comment_ids=[920],
        )

        with patch.object(config, "AUTO_MERGE", True), \
             patch.object(config, "IN_REVIEW_DEBOUNCE_SECONDS", 600):
            mocks = self._run(
                lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
                run_agent=_agent(
                    session_id="dev-sess", last_message="ack",
                ),
                push_branch=True,
                head_shas=["aaa", "bbb"],
            )

        # AUTO_MERGE must NOT fire over the human's id=910 comment.
        self.assertEqual(gh.merge_calls, [])
        self.assertNotIn((600, "done"), gh.label_history)
        # Dev resumed on the human comment.
        self.assertEqual(mocks["run_agent"].call_count, 1)
        self.assertIn(
            "do not merge yet",
            mocks["run_agent"].call_args.args[1],
        )
        self.assertIn((600, "validating"), gh.label_history)


class StaleParkReasonClearedOnNewParkTest(
    unittest.TestCase, _PatchedWorkflowMixin
):
    """A transient AUTO_MERGE park (failed_checks/unmergeable) followed by
    a comment-driven dev resume that itself parks (e.g. the dev asked a
    question, made no commit, or left a dirty worktree) must replace the
    stale `park_reason`. Otherwise the next tick's recovery branch sees a
    transient reason, re-checks gates, and merges over the dev's standing
    question or follow-up.
    """

    PR_NUMBER = 1200
    BRANCH = "orchestrator/issue-700"

    def test_stale_park_reason_cleared_after_question_park(self) -> None:
        gh = FakeGitHubClient()
        long_ago = datetime.now(timezone.utc) - timedelta(hours=1)
        # Tick 0 already parked for failed_checks; the human posted a
        # follow-up comment ("any update?") to nudge the orchestrator.
        issue = make_issue(700, label="in_review", comments=[
            FakeComment(
                id=3000, body="any update?",
                user=FakeUser("alice"), created_at=long_ago,
            ),
        ])
        gh.add_issue(issue)
        pr = FakePR(
            number=self.PR_NUMBER, head_branch=self.BRANCH,
            head=FakePRRef(sha="cafe1234"),
            mergeable=True, check_state="success",
        )
        gh.add_pr(pr)
        gh.seed_state(
            700,
            pr_number=self.PR_NUMBER, branch=self.BRANCH,
            dev_agent="claude", dev_session_id="dev-sess",
            agent_approved_sha="cafe1234",
            pr_last_comment_id=2999,
            pr_last_review_comment_id=0,
            pr_last_review_summary_id=0,
            # Carryover from the original transient park.
            awaiting_human=True,
            park_reason="failed_checks",
        )

        # Tick A: the new comment arrives; dev gets resumed; the run
        # produces no commit (head SHA unchanged), which routes through
        # `_on_question`. That path must clear `park_reason`.
        with patch.object(config, "AUTO_MERGE", True), \
             patch.object(config, "IN_REVIEW_DEBOUNCE_SECONDS", 600):
            self._run(
                lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
                run_agent=_agent(
                    session_id="dev-sess",
                    last_message="I cannot proceed without a clarification",
                ),
                push_branch=True,
                head_shas=["sha-before", "sha-before"],  # no new commit
            )
        data = gh.pinned_data(700)
        self.assertTrue(
            data.get("awaiting_human"),
            "should still be awaiting human after the question",
        )
        self.assertIsNone(
            data.get("park_reason"),
            "stale 'failed_checks' park reason must be cleared by the "
            "question park",
        )

        # Tick B: no new comments; gates still pass. Recovery must NOT
        # fire because park_reason is no longer transient.
        with patch.object(config, "AUTO_MERGE", True), \
             patch.object(config, "IN_REVIEW_DEBOUNCE_SECONDS", 600):
            self._run(
                lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
                run_agent=_agent(),
            )

        self.assertEqual(
            gh.merge_calls, [],
            "auto-merge must not fire over the standing dev question",
        )
        self.assertNotIn((700, "done"), gh.label_history)
        data = gh.pinned_data(700)
        self.assertTrue(data.get("awaiting_human"))


class CheckRunsForbiddenSurfacesScopeHintTest(unittest.TestCase):
    """A 403 from the check-runs endpoint almost always means the PAT is
    missing 'Checks: read'. Silently swallowing the exception leaves
    `pr_combined_check_state` at 'none' for Actions-only PRs and AUTO_MERGE
    parks forever. Promote the 403 to log.error with a specific message
    naming the scope.
    """

    def test_403_on_get_check_runs_logs_actionable_error(self) -> None:
        from unittest.mock import MagicMock
        from orchestrator.github import GitHubClient
        from github import GithubException

        client = GitHubClient.__new__(GitHubClient)
        client.repo = MagicMock()

        commit_obj = MagicMock()
        # Combined-status path returns nothing useful (Actions-only PR).
        combined = MagicMock(state="", total_count=0)
        commit_obj.get_combined_status.return_value = combined
        # Check-runs path raises 403.
        commit_obj.get_check_runs.side_effect = GithubException(
            403, {"message": "Resource not accessible"}, None,
        )
        client.repo.get_commit.return_value = commit_obj

        pr = MagicMock()
        pr.head.sha = "deadbeef"

        with self.assertLogs("orchestrator.github", level="ERROR") as cm:
            state = client.pr_combined_check_state(pr)

        self.assertEqual(state, "none")
        joined = "\n".join(cm.output)
        self.assertIn("403", joined)
        self.assertIn("Checks: read", joined)
        self.assertIn("AUTO_MERGE", joined)

    def test_non_403_check_runs_failure_logs_warning_only(self) -> None:
        # 404, transient 5xx, etc. are logged at warning level and don't
        # need scope guidance. Avoid noisy ERROR for unrelated failures.
        from unittest.mock import MagicMock
        from orchestrator.github import GitHubClient
        from github import GithubException

        client = GitHubClient.__new__(GitHubClient)
        client.repo = MagicMock()
        commit_obj = MagicMock()
        commit_obj.get_combined_status.return_value = MagicMock(
            state="", total_count=0
        )
        commit_obj.get_check_runs.side_effect = GithubException(
            500, {"message": "Internal Server Error"}, None,
        )
        client.repo.get_commit.return_value = commit_obj
        pr = MagicMock()
        pr.head.sha = "deadbeef"

        with self.assertLogs("orchestrator.github", level="WARNING") as cm:
            client.pr_combined_check_state(pr)

        # Filter to only WARNING records (assertLogs catches WARNING and above).
        warning_only = [r for r in cm.records if r.levelname == "WARNING"]
        self.assertTrue(warning_only, "should log a warning for non-403 errors")
        # No ERROR for non-403 failures.
        error_records = [r for r in cm.records if r.levelname == "ERROR"]
        self.assertEqual(error_records, [])


class AutoMergeSHAShiftDuringMergeabilityCheckTest(
    unittest.TestCase, _PatchedWorkflowMixin
):
    """`gh.pr_is_mergeable(pr)` calls `pr.update()` when the cached
    mergeable is None, which can refresh `pr.head.sha`. The approval and
    changes-requested gates ran against the earlier head_sha, so a commit
    landing during that refresh must NOT slip through to the merge call:
    AUTO_MERGE must NOT merge the refreshed (unreviewed) head.
    """

    PR_NUMBER = 30
    BRANCH = "orchestrator/issue-7"

    def _setup(self):
        gh = FakeGitHubClient()
        long_ago = datetime.now(timezone.utc) - timedelta(hours=1)
        issue = make_issue(7, label="in_review", comments=[
            FakeComment(
                id=900, body=":robot: orchestrator picking this up.",
                user=FakeUser("orchestrator"), created_at=long_ago,
            ),
            FakeComment(
                id=901, body=":sparkles: PR opened: #30",
                user=FakeUser("orchestrator"), created_at=long_ago,
            ),
        ])
        gh.add_issue(issue)
        pr = FakePR(
            number=self.PR_NUMBER, head_branch=self.BRANCH,
            head=FakePRRef(sha="reviewedSHA"),
            mergeable=True, check_state="success",
        )
        gh.add_pr(pr)
        gh.seed_state(
            7, pr_number=self.PR_NUMBER, branch=self.BRANCH,
            dev_agent="claude", dev_session_id="dev-sess",
            review_round=0,
            orchestrator_comment_ids=[900, 901],
            pickup_comment_id=900,
            agent_approved_sha="reviewedSHA",
            pr_last_comment_id=999,
            pr_last_review_comment_id=0,
            pr_last_review_summary_id=0,
        )
        return gh, issue, pr

    def test_sha_shift_during_pr_is_mergeable_blocks_merge(self) -> None:
        gh, issue, pr = self._setup()

        # Simulate what GitHub's lazy `pr.update()` does inside
        # `pr_is_mergeable`: a commit landed between the gate checks and
        # the mergeability resolution, so the refresh moves pr.head.sha to
        # an UNREVIEWED commit. The approval gate already ran against
        # 'reviewedSHA'; the merge must NOT proceed against 'unreviewedSHA'.
        def mergeable_with_refresh(pr_arg):
            pr_arg.head = FakePRRef(sha="unreviewedSHA")
            return True

        with patch.object(config, "AUTO_MERGE", True), \
             patch.object(config, "IN_REVIEW_DEBOUNCE_SECONDS", 600), \
             patch.object(gh, "pr_is_mergeable", mergeable_with_refresh):
            self._run(
                lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
                run_agent=_agent(),
            )

        # Critical: no merge happened. Without the SHA-shift bail (and the
        # head_sha pin on merge_pr), AUTO_MERGE would have called
        # merge_pr(pr, sha='unreviewedSHA') and merged the unreviewed head.
        self.assertEqual(
            gh.merge_calls, [],
            "merge must not fire when pr.head.sha shifted between the "
            "approval gate and the merge call",
        )
        # Issue stayed in_review; next tick will re-evaluate against the
        # new head SHA (which is not yet approved).
        self.assertNotIn((7, "done"), gh.label_history)

    def test_sha_unchanged_during_pr_is_mergeable_merges_normally(self) -> None:
        # Sanity check: the SHA-shift guard must not regress the happy path
        # when `pr_is_mergeable` does NOT refresh the head. Same setup but
        # without the head mutation.
        gh, issue, pr = self._setup()

        with patch.object(config, "AUTO_MERGE", True), \
             patch.object(config, "IN_REVIEW_DEBOUNCE_SECONDS", 600):
            self._run(
                lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
                run_agent=_agent(),
            )

        self.assertEqual(
            gh.merge_calls, [(self.PR_NUMBER, "reviewedSHA", "squash")],
            "happy path must still merge against the gated head_sha",
        )
        self.assertIn((7, "done"), gh.label_history)


class PrCombinedCheckStatePartialReadFailsClosedTest(unittest.TestCase):
    """A read failure on one checks surface must NOT be masked by a
    'success' from the other surface. Otherwise a single green
    commit-status context plus failing or pending GitHub Actions check-runs
    that the PAT cannot read (403 from a missing 'Checks: read' scope, or a
    transient 5xx) would be reported as 'success' and AUTO_MERGE could land
    a PR over the unread failing checks.
    """

    def _client_with(self, *, combined_state, combined_total, check_runs_exc):
        from unittest.mock import MagicMock
        from orchestrator.github import GitHubClient

        client = GitHubClient.__new__(GitHubClient)
        client.repo = MagicMock()
        commit_obj = MagicMock()
        commit_obj.get_combined_status.return_value = MagicMock(
            state=combined_state, total_count=combined_total,
        )
        commit_obj.get_check_runs.side_effect = check_runs_exc
        client.repo.get_commit.return_value = commit_obj
        pr = MagicMock()
        pr.head.sha = "deadbeef"
        return client, pr

    def test_combined_success_with_check_runs_403_returns_pending(self) -> None:
        # The dangerous case: legacy commit-status says 'success' but the
        # PAT cannot read check-runs. Without the partial-read guard,
        # AUTO_MERGE would land over failing/pending Actions runs.
        from github import GithubException

        client, pr = self._client_with(
            combined_state="success", combined_total=1,
            check_runs_exc=GithubException(
                403, {"message": "Resource not accessible"}, None,
            ),
        )
        with self.assertLogs("orchestrator.github", level="ERROR"):
            state = client.pr_combined_check_state(pr)
        self.assertEqual(
            state, "pending",
            "partial read with combined='success' must downgrade to "
            "'pending' to keep AUTO_MERGE from merging on half the picture",
        )

    def test_combined_success_with_check_runs_500_returns_pending(self) -> None:
        # A transient 5xx on check-runs has the same downgrade rule -- the
        # next tick may succeed and resolve to a real verdict, but until
        # then we cannot report success.
        from github import GithubException

        client, pr = self._client_with(
            combined_state="success", combined_total=1,
            check_runs_exc=GithubException(
                500, {"message": "Internal Server Error"}, None,
            ),
        )
        with self.assertLogs("orchestrator.github", level="WARNING"):
            state = client.pr_combined_check_state(pr)
        self.assertEqual(state, "pending")

    def test_no_combined_signal_with_check_runs_403_still_returns_none(self) -> None:
        # Edge case: combined-status returned no usable signal AND
        # check-runs raised. We have NO signal at all; preserve the
        # existing 'none' return so the workflow's failed_checks branch
        # parks awaiting_human (visible to the operator) instead of
        # silently waiting forever on 'pending'.
        from github import GithubException

        client, pr = self._client_with(
            combined_state="", combined_total=0,
            check_runs_exc=GithubException(
                403, {"message": "Resource not accessible"}, None,
            ),
        )
        with self.assertLogs("orchestrator.github", level="ERROR"):
            state = client.pr_combined_check_state(pr)
        self.assertEqual(
            state, "none",
            "no signal on either surface must keep returning 'none' so "
            "the workflow parks awaiting_human instead of pending forever",
        )


class HandleInReviewResumeOnHashChangeTest(
    unittest.TestCase, _PatchedWorkflowMixin,
):
    def test_body_drift_resumes_dev_and_bounces_to_validating(self) -> None:
        # The in_review handler must mirror the comment-driven dev resume:
        # post a notice on the PR (not just the issue), resume the locked
        # dev session with the new body, push the fix, and bounce back to
        # `validating` so the reviewer re-runs.
        gh = FakeGitHubClient()
        issue = make_issue(80, label="in_review", body="new acceptance")
        gh.add_issue(issue)
        pr = FakePR(number=800, head_branch="orchestrator/issue-80")
        gh.add_pr(pr)
        gh.seed_state(
            80,
            user_content_hash="stale-hash",
            dev_agent="claude",
            dev_session_id="dev-sess",
            pr_number=pr.number,
            pr_last_comment_id=0,
            pr_last_review_comment_id=0,
            pr_last_review_summary_id=0,
            branch="orchestrator/issue-80",
        )

        self._run(
            lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
            run_agent=_agent(
                session_id="dev-sess", last_message="addressed"
            ),
            has_new_commits=True,
            dirty_files=(),
            push_branch=True,
            head_shas=["before", "after"],
        )

        # Bounced back to validating after a successful resume.
        self.assertIn((80, "validating"), gh.label_history)
        # Notice posted on the PR conversation surface.
        self.assertTrue(any(
            "issue body changed" in body
            for _, body in gh.posted_pr_comments
        ))
        data = gh.pinned_data(80)
        # New hash persisted.
        self.assertNotEqual(data.get("user_content_hash"), "stale-hash")
        # review_round reset because this is a new diff.
        self.assertEqual(data.get("review_round"), 0)


class InReviewDriftForwardsUnreadPrConversationTest(
    unittest.TestCase, _PatchedWorkflowMixin,
):
    """Reviewer concern: the in_review drift path resumes the dev with
    `_recent_comments_text(issue)` (issue thread only), then
    `_bump_in_review_watermarks` advances the shared `pr_last_comment_id`
    based on the issue-thread max. A PR-conversation comment whose id
    falls between the prior watermark and the issue-thread max would be
    silently consumed by the bump and never forwarded to the dev. The
    drift path must capture unread PR comments BEFORE the bump and
    include them in the followup prompt."""

    def test_unread_pr_comment_below_issue_max_is_forwarded(
        self,
    ) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(
            1300, label="in_review", body="updated body",
        )
        # Issue-thread comment with id 200 (the body-edit signal).
        issue.comments.append(FakeComment(
            id=200, body="adds an acceptance criterion",
            user=FakeUser("alice"),
        ))
        gh.add_issue(issue)
        pr = FakePR(number=13000, head_branch="orchestrator/issue-1300")
        # Concurrent PR-conversation comment at id 150 (between the
        # prior watermark and the issue-thread max). Without the fix,
        # the watermark bump leaps to 200 and this comment is lost.
        pr.issue_comments.append(FakeComment(
            id=150, body="please also handle empty input",
            user=FakeUser("alice"),
        ))
        gh.add_pr(pr)
        gh.seed_state(
            1300,
            pr_number=pr.number,
            dev_agent="claude",
            dev_session_id="dev-sess",
            user_content_hash="stale-hash",
            pr_last_comment_id=100,
            pr_last_review_comment_id=0,
            pr_last_review_summary_id=0,
            branch="orchestrator/issue-1300",
            last_action_comment_id=100,
        )

        mocks = self._run(
            lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
            run_agent=_agent(
                session_id="dev-sess", last_message="addressed both",
            ),
            has_new_commits=True,
            dirty_files=(),
            push_branch=True,
            head_shas=["before", "after"],
        )

        # The dev's prompt must include the unread PR-conversation
        # comment so it is not lost to the watermark bump.
        prompt = mocks["run_agent"].call_args[0][1]
        self.assertIn("please also handle empty input", prompt)
        # The bump advanced past BOTH the issue-thread max AND the PR
        # comment, so the next tick won't replay either.
        data = gh.pinned_data(1300)
        self.assertGreaterEqual(
            int(data.get("pr_last_comment_id")), 200,
        )

    def test_unread_pr_comment_above_issue_max_also_consumed(
        self,
    ) -> None:
        # Symmetric guard: a PR-conversation comment whose id is HIGHER
        # than every issue-thread id must also be consumed by the bump
        # (we forward it to the dev AND include it in `issue_space_new`
        # so the bump's candidate set extends past it).
        gh = FakeGitHubClient()
        issue = make_issue(1310, label="in_review", body="updated body")
        gh.add_issue(issue)
        pr = FakePR(number=13100, head_branch="orchestrator/issue-1310")
        pr.issue_comments.append(FakeComment(
            id=600, body="additional ask",
            user=FakeUser("alice"),
        ))
        gh.add_pr(pr)
        gh.seed_state(
            1310,
            pr_number=pr.number,
            dev_agent="claude",
            dev_session_id="dev-sess",
            user_content_hash="stale-hash",
            pr_last_comment_id=100,
            pr_last_review_comment_id=0,
            pr_last_review_summary_id=0,
            branch="orchestrator/issue-1310",
            last_action_comment_id=100,
        )

        mocks = self._run(
            lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
            run_agent=_agent(
                session_id="dev-sess", last_message="done"
            ),
            has_new_commits=True,
            dirty_files=(),
            push_branch=True,
            head_shas=["before", "after"],
        )

        prompt = mocks["run_agent"].call_args[0][1]
        self.assertIn("additional ask", prompt)
        data = gh.pinned_data(1310)
        self.assertGreaterEqual(
            int(data.get("pr_last_comment_id")), 600,
        )
