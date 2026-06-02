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

    def test_in_review_mergeable_pings_human(self) -> None:
        # PR mergeable: post a one-shot HITL ping so the human knows the
        # PR is ready, but stay open (no merge, no label flip, no
        # awaiting_human). The orchestrator is manual-merge-only -- it
        # never calls `gh.merge_pr` from in_review. The ping must mention
        # every HITL handle so notifications fire even on a multi-handle
        # deployment.
        pr = self._open_pr(approved=True, mergeable=True, check_state="success")
        gh, issue = self._seed(pr=pr)

        with patch.object(config, "HITL_MENTIONS", "@alice @bob"):
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

    def test_in_review_mergeable_unapproved_does_not_ping(self) -> None:
        # The ping advertises the PR as ready for review/merge; firing it
        # on a mergeable-but-unapproved PR would invite a manual merge
        # over a commit no reviewer has signed off on. The gate must
        # require approval (agent_approved_sha for the current head, OR
        # a human APPROVED review on the current head).
        pr = self._open_pr(approved=False, mergeable=True, check_state="success")
        gh, issue = self._seed(pr=pr)

        self._run(
            lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
            run_agent=_agent(),
        )

        self.assertEqual(gh.merge_calls, [])
        self.assertEqual(gh.label_history, [])
        self.assertEqual(gh.posted_comments, [])
        data = gh.pinned_data(30)
        self.assertIsNone(data.get("ready_ping_sha"))
        self.assertFalse(data.get("awaiting_human"))

    def test_in_review_mergeable_stale_agent_approval_does_not_ping(self) -> None:
        # `agent_approved_sha` snapshotted the head the reviewer agent
        # OK'd; a later push shifts pr.head.sha and the snapshot no
        # longer matches. The ping must wait for the next reviewer round
        # to re-approve the new head.
        pr = self._open_pr(
            approved=False, mergeable=True, check_state="success",
            head=FakePRRef(sha="newhead99"),
        )
        gh, issue = self._seed(
            pr=pr, extra_state={"agent_approved_sha": "cafe1234"},
        )

        self._run(
            lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
            run_agent=_agent(),
        )

        self.assertEqual(gh.merge_calls, [])
        self.assertEqual(gh.posted_comments, [])
        data = gh.pinned_data(30)
        self.assertIsNone(data.get("ready_ping_sha"))

    def test_in_review_mergeable_changes_requested_does_not_ping(self) -> None:
        # A standing human CHANGES_REQUESTED on the current head vetoes
        # the ping even when the reviewer agent approved the same SHA;
        # the orchestrator must not advertise the PR as ready while a
        # human review is asking for changes.
        pr = self._open_pr(
            approved=True, mergeable=True, check_state="success",
            changes_requested=True,
        )
        gh, issue = self._seed(
            pr=pr, extra_state={"agent_approved_sha": "cafe1234"},
        )

        self._run(
            lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
            run_agent=_agent(),
        )

        self.assertEqual(gh.merge_calls, [])
        self.assertEqual(gh.posted_comments, [])
        data = gh.pinned_data(30)
        self.assertIsNone(data.get("ready_ping_sha"))

    def test_in_review_mergeable_dedups_same_head(self) -> None:
        # Second tick on the same head SHA must NOT re-ping; the ping is
        # one-shot per head so a long-lived ready-for-merge PR doesn't spam
        # the HITL handles on every poll.
        pr = self._open_pr(approved=True, mergeable=True, check_state="success")
        gh, issue = self._seed(pr=pr)

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

    def test_in_review_mergeable_repings_new_head(self) -> None:
        # A new commit on the PR branch shifts pr.head.sha; the ping is
        # keyed on the SHA we last pinged for, so the next tick must
        # re-ping on the new head.
        pr = self._open_pr(approved=True, mergeable=True, check_state="success")
        gh, issue = self._seed(pr=pr)

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

        with _patch_mock.object(workflow, "_post_issue_comment", post_with_race):
            self._run(
                lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
                run_agent=_agent(),
            )

        # Watermark must NOT have advanced past the human comment.
        data = gh.pinned_data(30)
        self.assertLess(data.get("pr_last_comment_id"), human.id)

        # Second tick: the human comment surfaces. The fresh-feedback
        # scan now runs BEFORE the drift check, so the human comment
        # routes the issue to `fixing` (the dev is not spawned by
        # `_handle_in_review` here). The ping itself is filtered as
        # orchestrator-authored, so the route is driven by the (real,
        # human-authored) `human` comment.
        mocks = self._run(
            lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
            run_agent=_agent(),
        )
        mocks["run_agent"].assert_not_called()
        self.assertIn((30, "fixing"), gh.label_history)
        self.assertEqual(
            gh.pinned_data(30).get("pending_fix_issue_max_id"), human.id,
        )

    def test_in_review_hold_base_sync_pauses_hitl_ping(self) -> None:
        # BASE_SYNC_HOLD label suppresses the HITL ping so the operator's
        # in-progress base sync can finish without spamming the issue
        # thread.
        pr = self._open_pr(approved=True, mergeable=True, check_state="success")
        gh, issue = self._seed(pr=pr)
        issue.labels.append(FakeLabel(BASE_SYNC_HOLD_LABEL))

        mocks = self._run(
            lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
            run_agent=_agent(),
        )

        mocks["run_agent"].assert_not_called()
        self.assertEqual(gh.merge_calls, [])
        self.assertEqual(gh.label_history, [])
        self.assertEqual(gh.posted_comments, [])
        self.assertNotIn("merged_at", gh.pinned_data(30))

    def test_in_review_unmergeable_parks_for_human(self) -> None:
        # PR not mergeable: park awaiting human with
        # `park_reason="unmergeable"`. The orchestrator never routes from
        # in_review to `resolving_conflict`; the human drives the merge
        # (or relabels manually).
        pr = self._open_pr(approved=True, mergeable=False, check_state="success")
        gh, issue = self._seed(pr=pr)

        mocks = self._run(
            lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
            run_agent=_agent(),
        )

        mocks["run_agent"].assert_not_called()
        self.assertEqual(gh.merge_calls, [])
        # Must NOT route to resolving_conflict.
        self.assertNotIn((30, "resolving_conflict"), gh.label_history)
        data = gh.pinned_data(30)
        self.assertTrue(data.get("awaiting_human"))
        self.assertEqual(data.get("park_reason"), "unmergeable")
        last_comment = gh.posted_comments[-1][1]
        self.assertIn("not mergeable", last_comment)
        # No conflict_round seeded -- the orchestrator never enters the
        # auto-resolution route from here.
        self.assertNotIn("conflict_round", data)

    def test_in_review_hold_base_sync_skips_unmergeable_park(self) -> None:
        pr = self._open_pr(approved=True, mergeable=False, check_state="success")
        gh, issue = self._seed(pr=pr)
        issue.labels.append(FakeLabel(BASE_SYNC_HOLD_LABEL))

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

    def test_in_review_mergeable_pending(self) -> None:
        # mergeable=None means GitHub is still computing. Don't ping,
        # don't park; the next tick re-checks once GitHub has decided.
        pr = self._open_pr(approved=True, mergeable=None, check_state="success")
        gh, issue = self._seed(pr=pr)

        self._run(
            lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
            run_agent=_agent(),
        )

        self.assertEqual(gh.merge_calls, [])
        self.assertEqual(gh.label_history, [])
        self.assertEqual(gh.posted_comments, [])
        self.assertFalse(gh.pinned_data(30).get("awaiting_human"))

    def test_in_review_pr_comment_within_debounce_flips_to_fixing(self) -> None:
        # Fresh PR feedback inside the debounce window must NOT silently
        # wait or spawn the dev: the handler records pending-fix metadata
        # and flips the label to `fixing` immediately so the fixing handler
        # can own its own debounce / resume cycle.
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

        with patch.object(config, "IN_REVIEW_DEBOUNCE_SECONDS", 600):
            mocks = self._run(
                lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
                run_agent=_agent(),
            )

        # No dev spawn, no merge attempt (the in_review handler is not the
        # one that drives the fix any more); label flipped to `fixing`.
        mocks["run_agent"].assert_not_called()
        self.assertEqual(gh.merge_calls, [])
        self.assertIn((30, "fixing"), gh.label_history)
        data = gh.pinned_data(30)
        self.assertIn("pending_fix_at", data)
        self.assertEqual(data.get("pending_fix_issue_max_id"), 2000)
        # Watermarks deliberately NOT bumped: the fixing handler needs the
        # triggering comments to build its dev-resume prompt.
        self.assertEqual(data.get("pr_last_comment_id"), 1999)

    def test_in_review_pr_comment_past_debounce_flips_to_fixing(self) -> None:
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
            run_agent=_agent(),
        )

        # Past-debounce feedback also hands off to the fixing stage rather
        # than spawning the dev inline. The fixing handler owns the
        # resume / push / hand-back-to-`validating` cycle (a pushed fix
        # flips DIRECTLY back to `validating` for the reviewer to
        # re-evaluate; docs do not run here, the single docs pass runs
        # after reviewer approval before `in_review`).
        mocks["run_agent"].assert_not_called()
        mocks["_push_branch"].assert_not_called()
        self.assertIn((30, "fixing"), gh.label_history)
        self.assertNotIn((30, "validating"), gh.label_history)
        data = gh.pinned_data(30)
        self.assertIn("pending_fix_at", data)
        self.assertEqual(data.get("pending_fix_issue_max_id"), 2000)

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


class InReviewParkWatermarkTest(unittest.TestCase, _PatchedWorkflowMixin):
    """A park inside `_handle_in_review` posts an issue comment. The watermark
    must be bumped past that comment so the next tick does not see the
    orchestrator's own HITL park message as fresh PR feedback and route
    the issue to `fixing` against it.
    """

    def test_unmergeable_park_does_not_replay_on_next_tick(self) -> None:
        # An unmergeable PR parks awaiting human on the first tick. The
        # park message is recorded as orchestrator-authored and the
        # watermark is bumped past it; subsequent ticks must not surface
        # the park message as fresh PR feedback.
        gh = FakeGitHubClient()
        issue = make_issue(60, label="in_review")
        gh.add_issue(issue)
        pr = FakePR(
            number=70, head_branch="orchestrator/issue-60",
            head=FakePRRef(sha="cafe1234"),
            approved=True, approval_head_sha="cafe1234",
            mergeable=False, check_state="success",
        )
        gh.add_pr(pr)
        gh.seed_state(
            60, pr_number=70, branch="orchestrator/issue-60",
            dev_agent="claude", dev_session_id="dev-sess",
            pr_last_comment_id=900,  # an old watermark from validating handoff
        )

        # Tick 1: unmergeable park.
        self._run(
            lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
            run_agent=_agent(),
        )
        self.assertTrue(gh.pinned_data(60).get("awaiting_human"))
        self.assertEqual(gh.pinned_data(60).get("park_reason"), "unmergeable")
        comments_after_park = len(gh.posted_comments)
        self.assertGreater(comments_after_park, 0)
        # Watermark must have been bumped past the park comment -- which
        # means it's at or above the latest comment id on the issue.
        latest_id = gh.latest_comment_id(issue)
        self.assertEqual(gh.pinned_data(60).get("pr_last_comment_id"), latest_id)

        # Tick 2: nothing new; must NOT route the orchestrator's park
        # message back through the fixing route.
        mocks = self._run(
            lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
            run_agent=_agent(),
        )
        mocks["run_agent"].assert_not_called()
        # No additional comments posted (no second park, no fixing route).
        self.assertEqual(len(gh.posted_comments), comments_after_park)
        self.assertNotIn((60, "fixing"), gh.label_history)


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

    def test_inline_review_comment_routes_to_fixing(self) -> None:
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
            run_agent=_agent(),
        )

        mocks["run_agent"].assert_not_called()
        self.assertIn((65, "fixing"), gh.label_history)
        self.assertNotIn((65, "validating"), gh.label_history)
        data = gh.pinned_data(65)
        # Bookmark recorded but the inline-review watermark stays where it
        # was -- the fixing handler needs the triggering comment.
        self.assertEqual(data.get("pending_fix_review_max_id"), 42)
        self.assertEqual(data.get("pr_last_review_comment_id"), 41)

    def test_id_overlap_across_spaces_does_not_drop_comments(self) -> None:
        # Inline review comment id (5) is LOWER than the issue-comment
        # watermark (1000). With one merged-id watermark this comment would
        # be silently filtered out; with split watermarks it gets through
        # and triggers the route to `fixing`.
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
            run_agent=_agent(),
        )

        # The inline comment surfaces and routes to fixing even though
        # id=5 < pr_last_comment_id=1000.
        mocks["run_agent"].assert_not_called()
        self.assertIn((65, "fixing"), gh.label_history)
        self.assertEqual(gh.pinned_data(65).get("pending_fix_review_max_id"), 5)


class InReviewPRReviewSummaryTest(unittest.TestCase, _PatchedWorkflowMixin):
    """A human can leave PR feedback either through inline review comments
    or through the *review summary* body (the textbox above the
    Approve / Request Changes / Comment buttons). The summary lives in the
    PullRequestReview id namespace, distinct from issue comments and inline
    review comments. Without surfacing it, a "Comment" review with body or
    a CHANGES_REQUESTED summary would never be routed to `fixing` -- the
    dev would never see the feedback.
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

    def test_changes_requested_with_body_routes_to_fixing(self) -> None:
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

        with patch.object(config, "IN_REVIEW_DEBOUNCE_SECONDS", 600):
            mocks = self._run(
                lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
                run_agent=_agent(),
            )

        # The CHANGES_REQUESTED review surfaces as fresh feedback and the
        # handler flips to `fixing` without spawning the dev or merging.
        mocks["run_agent"].assert_not_called()
        self.assertIn((90, "fixing"), gh.label_history)
        self.assertNotIn((90, "validating"), gh.label_history)
        self.assertEqual(gh.merge_calls, [])
        data = gh.pinned_data(90)
        self.assertEqual(data.get("pending_fix_review_summary_max_id"), 4242)
        # Watermark stays put so the fixing handler can read the review
        # body when it builds its dev-resume prompt.
        self.assertEqual(data.get("pr_last_review_summary_id"), 0)

    def test_commented_review_with_body_routes_to_fixing(self) -> None:
        # A "Comment" review (state=COMMENTED) needs to surface as fresh
        # feedback even though it does not block via
        # pr_has_changes_requested -- without the route to `fixing` the
        # human's note would never reach the dev session.
        long_ago = datetime.now(timezone.utc) - timedelta(hours=1)
        review = FakePRReview(
            id=4243,
            body="how about adding a smoke test for the empty-input case?",
            state="COMMENTED",
            user=FakeUser("alice"),
            submitted_at=long_ago,
        )
        gh, issue, pr = self._setup_with_review(review)

        with patch.object(config, "IN_REVIEW_DEBOUNCE_SECONDS", 600):
            mocks = self._run(
                lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
                run_agent=_agent(),
            )

        mocks["run_agent"].assert_not_called()
        self.assertEqual(gh.merge_calls, [])
        self.assertIn((90, "fixing"), gh.label_history)
        self.assertEqual(
            gh.pinned_data(90).get("pending_fix_review_summary_max_id"),
            4243,
        )

    def test_approved_review_body_does_not_trigger_resume(self) -> None:
        # APPROVED reviews are excluded from the summary surface even when
        # they carry an informational body. The human approved the PR --
        # their note is not a request for changes, so the handler must not
        # route to `fixing` and must instead ping the HITL handles for
        # manual merge.
        long_ago = datetime.now(timezone.utc) - timedelta(hours=1)
        review = FakePRReview(
            id=4244, body="LGTM, ship it", state="APPROVED",
            user=FakeUser("alice"), submitted_at=long_ago,
        )
        gh, issue, pr = self._setup_with_review(review)
        pr.approved = True
        pr.approval_head_sha = "cafe1234"

        with patch.object(config, "IN_REVIEW_DEBOUNCE_SECONDS", 600):
            mocks = self._run(
                lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
                run_agent=_agent(),
            )

        mocks["run_agent"].assert_not_called()
        # No fixing route; the handler pings HITL for a manual merge.
        self.assertEqual(gh.merge_calls, [])
        self.assertNotIn((90, "fixing"), gh.label_history)
        self.assertNotIn((90, "done"), gh.label_history)
        ping_comments = [
            body for _, body in gh.posted_comments
            if "ready for review/merge" in body
        ]
        self.assertEqual(len(ping_comments), 1)

    def test_empty_body_review_is_ignored(self) -> None:
        # A CHANGES_REQUESTED review with no body has nothing to forward to
        # the dev. The handler must not route to `fixing` for the empty
        # body; it falls through to the normal mergeable / ping path
        # (mergeable=True here -> the HITL ping fires).
        long_ago = datetime.now(timezone.utc) - timedelta(hours=1)
        review = FakePRReview(
            id=4245, body="", state="CHANGES_REQUESTED",
            user=FakeUser("alice"), submitted_at=long_ago,
        )
        gh, issue, pr = self._setup_with_review(review)
        pr.changes_requested = True
        pr.changes_requested_head_sha = "cafe1234"

        with patch.object(config, "IN_REVIEW_DEBOUNCE_SECONDS", 600):
            mocks = self._run(
                lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
                run_agent=_agent(),
            )

        mocks["run_agent"].assert_not_called()
        self.assertEqual(gh.merge_calls, [])
        self.assertNotIn((90, "fixing"), gh.label_history)


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
            agent_approved_sha="cafe1234",
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


class LegacyInReviewWatermarkSeedTest(unittest.TestCase, _PatchedWorkflowMixin):
    """An issue that reached `in_review` before validating started seeding
    watermarks (or that was manually relabeled, or whose handoff failed to
    snapshot the PR) sits on the in_review handler with all three watermarks
    unset. Without the first-tick migration, every historical comment --
    including the orchestrator's own pickup / PR-opened / approval messages
    -- would surface as fresh PR feedback once the debounce expired,
    routing the issue to `fixing` (and back to `validating` on the
    eventual pushed fix).
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

        with patch.object(config, "IN_REVIEW_DEBOUNCE_SECONDS", 600):
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

    def test_legacy_first_tick_pings_hitl_for_mergeable_pr(self) -> None:
        # All gates passing: the migration must not park or otherwise
        # block the handler from posting the HITL ping -- it only treats
        # already-visible comments as consumed.
        gh, issue, pr = self._legacy_setup()
        # Drop the historical review-summary so the summary watermark
        # seeds past the inline review and the handler reaches the
        # mergeable / ping path.
        pr.reviews = []

        with patch.object(config, "IN_REVIEW_DEBOUNCE_SECONDS", 600):
            self._run(
                lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
                run_agent=_agent(),
            )

        # No merge (humans drive the merge); HITL ping fires for the
        # mergeable PR.
        self.assertEqual(gh.merge_calls, [])
        self.assertNotIn((150, "done"), gh.label_history)
        ping_comments = [
            body for _, body in gh.posted_comments
            if "ready for review/merge" in body
        ]
        self.assertEqual(len(ping_comments), 1)
        self.assertEqual(
            gh.pinned_data(150).get("ready_ping_sha"), "cafe1234",
        )


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
            agent_approved_sha="cafe1234",
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


class AwaitingHumanParkStaysParkedTest(
    unittest.TestCase, _PatchedWorkflowMixin,
):
    """An issue parked awaiting human must stay parked when no new comments
    surface. The handler is manual-merge-only -- there is no auto-recovery
    branch that re-checks the mergeability gate. A human reply (comment or
    relabel) is what unsticks the issue.
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

    def test_unmergeable_park_stays_parked_when_pr_becomes_mergeable(
        self,
    ) -> None:
        # Even if the PR silently becomes mergeable (rebase resolved a
        # conflict, branch protection dropped), the handler does NOT
        # auto-recover -- the orchestrator never merges from in_review.
        # Park flags stay so the operator notices and drives the merge.
        gh, issue, pr = self._parked_issue(
            park_reason="unmergeable",
            pr_kwargs=dict(mergeable=True, check_state="success"),
        )

        mocks = self._run(
            lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
            run_agent=_agent(),
        )

        mocks["run_agent"].assert_not_called()
        self.assertEqual(gh.merge_calls, [])
        self.assertEqual(gh.label_history, [])
        # No new park comment posted on this tick.
        self.assertEqual(gh.posted_comments, [])
        # Park flags preserved.
        data = gh.pinned_data(170)
        self.assertTrue(data.get("awaiting_human"))
        self.assertEqual(data.get("park_reason"), "unmergeable")


class ManuallyClosedInReviewIssueTest(unittest.TestCase, _PatchedWorkflowMixin):
    """An open in_review issue closed manually by a human is a stop signal.
    The closed-in_review sweep yields the issue (so a Resolves-#N auto-close
    can finalize to `done`), but if the linked PR is still open the sweep
    has surfaced a manually-closed issue and `_handle_in_review` must mark
    it rejected before the mergeable / HITL-ping path runs.
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

        mocks = self._run(
            lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
            run_agent=_agent(),
        )

        # The handler must not fall through to the HITL ping over a
        # manually-closed issue even though the PR is otherwise mergeable.
        self.assertEqual(gh.merge_calls, [])
        self.assertIn((250, "rejected"), gh.label_history)
        self.assertNotIn((250, "done"), gh.label_history)
        self.assertIn("closed_without_merge_at", gh.pinned_data(250))
        self.assertEqual(gh.posted_comments, [])
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

        with patch.object(config, "IN_REVIEW_DEBOUNCE_SECONDS", 600):
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
    new_comments, silently swallowing that first review and skipping the
    `fixing` route. The migration must persist 0 even on empty surfaces
    so the next tick scans new comments instead of re-migrating.
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

        with patch.object(config, "IN_REVIEW_DEBOUNCE_SECONDS", 600):
            mocks = self._run(
                lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
                run_agent=_agent(),
            )

        # The first inline review comment after migration is treated as
        # fresh feedback and routes the issue to `fixing` (no dev spawn
        # here; the fixing handler owns that step).
        mocks["run_agent"].assert_not_called()
        self.assertEqual(gh.merge_calls, [])
        self.assertIn((400, "fixing"), gh.label_history)
        self.assertEqual(
            gh.pinned_data(400).get("pending_fix_review_max_id"), 42,
        )

    def test_first_review_summary_after_migration_surfaces(self) -> None:
        # Same shape on the review-summary surface. A COMMENTED summary
        # body must still surface through the fresh-feedback scan; without
        # the migration persisting 0, the body would be migrated past and
        # the human would never reach the dev.
        gh, issue, pr = self._legacy_setup()
        gh.seed_state(
            400, pr_number=self.PR_NUMBER, branch=self.BRANCH,
            dev_agent="claude", dev_session_id="dev-sess",
            agent_approved_sha="cafe1234",
        )

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

        with patch.object(config, "IN_REVIEW_DEBOUNCE_SECONDS", 600):
            mocks = self._run(
                lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
                run_agent=_agent(),
            )

        mocks["run_agent"].assert_not_called()
        self.assertEqual(gh.merge_calls, [])
        self.assertIn((400, "fixing"), gh.label_history)
        self.assertEqual(
            gh.pinned_data(400).get("pending_fix_review_summary_max_id"),
            5050,
        )


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
        implementing_label = MagicMock(name="implementing_label")
        documenting_label = MagicMock(name="documenting_label")
        validating_label = MagicMock(name="validating_label")
        in_review_label = MagicMock(name="in_review_label")
        fixing_label = MagicMock(name="fixing_label")
        resolving_label = MagicMock(name="resolving_conflict_label")
        question_label = MagicMock(name="question_label")

        def fake_get_label(name: str):
            return {
                "implementing": implementing_label,
                "documenting": documenting_label,
                "validating": validating_label,
                "in_review": in_review_label,
                "fixing": fixing_label,
                "resolving_conflict": resolving_label,
                "question": question_label,
            }[name]

        client.repo.get_label.side_effect = fake_get_label

        list(client.list_pollable_issues())

        # Each sweep label is looked up by name (one query per label
        # because the GitHub Issues API treats `labels` as AND, not OR --
        # a single query for "any of these labels" is impossible).
        looked_up = [
            ca.args[0] for ca in client.repo.get_label.call_args_list
        ]
        self.assertIn("implementing", looked_up)
        self.assertIn("documenting", looked_up)
        self.assertIn("validating", looked_up)
        self.assertIn("in_review", looked_up)
        self.assertIn("fixing", looked_up)
        self.assertIn("resolving_conflict", looked_up)
        self.assertIn("question", looked_up)
        # The closed sweeps were invoked with Label OBJECTS, not strings.
        closed_calls = [
            ca for ca in client.repo.get_issues.call_args_list
            if ca.kwargs.get("state") == "closed"
        ]
        self.assertEqual(len(closed_calls), 7)
        labels_passed = [ca.kwargs["labels"] for ca in closed_calls]
        self.assertIn([implementing_label], labels_passed)
        self.assertIn([documenting_label], labels_passed)
        self.assertIn([validating_label], labels_passed)
        self.assertIn([in_review_label], labels_passed)
        self.assertIn([fixing_label], labels_passed)
        self.assertIn([resolving_label], labels_passed)
        self.assertIn([question_label], labels_passed)

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
    time "do not merge yet") sits below the watermark and the in_review ->
    fixing route would silently skip it.
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

        with patch.object(config, "IN_REVIEW_DEBOUNCE_SECONDS", 600):
            mocks = self._run(
                lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
                run_agent=_agent(),
            )

        # No merge attempt; the human's id=910 comment surfaces as fresh
        # feedback and routes the issue to `fixing` (the in_review handler
        # no longer drives the dev resume itself).
        self.assertEqual(gh.merge_calls, [])
        self.assertNotIn((600, "done"), gh.label_history)
        mocks["run_agent"].assert_not_called()
        self.assertIn((600, "fixing"), gh.label_history)
        self.assertEqual(
            gh.pinned_data(600).get("pending_fix_issue_max_id"), 910,
        )


class StaleParkReasonClearedOnFixingRouteTest(
    unittest.TestCase, _PatchedWorkflowMixin
):
    """A transient in_review park (unmergeable) followed by a fresh PR
    comment must clear the stale `park_reason` and `awaiting_human` flags
    as part of the in_review -> fixing route so the fixing handler is not
    greeted with stale park state.
    """

    PR_NUMBER = 1200
    BRANCH = "orchestrator/issue-700"

    def test_stale_park_reason_cleared_on_route_to_fixing(self) -> None:
        gh = FakeGitHubClient()
        long_ago = datetime.now(timezone.utc) - timedelta(hours=1)
        # Tick 0 already parked for unmergeable; the human posted a
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
            park_reason="unmergeable",
        )

        # Tick A: the new comment arrives; the handler routes to `fixing`
        # and clears both the stale `park_reason` and `awaiting_human`
        # flag so the fixing handler is not greeted with stale park state.
        with patch.object(config, "IN_REVIEW_DEBOUNCE_SECONDS", 600):
            mocks = self._run(
                lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
                run_agent=_agent(),
            )

        mocks["run_agent"].assert_not_called()
        self.assertIn((700, "fixing"), gh.label_history)
        data = gh.pinned_data(700)
        self.assertFalse(
            data.get("awaiting_human"),
            "the route to fixing consumes the human signal and clears the "
            "stale awaiting_human flag",
        )
        self.assertIsNone(
            data.get("park_reason"),
            "stale 'unmergeable' park reason must be cleared by the route "
            "to fixing",
        )
        self.assertEqual(data.get("pending_fix_issue_max_id"), 3000)
        self.assertEqual(gh.merge_calls, [])


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
    def test_body_drift_pushed_bounces_directly_to_validating(
        self,
    ) -> None:
        # The in_review handler must mirror the comment-driven dev resume:
        # post a notice on the PR (not just the issue), resume the locked
        # dev session with the new body, push the fix, and bounce
        # DIRECTLY back to `validating` so the reviewer re-evaluates the
        # updated body / new head. Docs do not run on the drift exit --
        # the single docs pass runs after reviewer approval before
        # `in_review` via the final-docs handoff, so running the docs
        # stage against an unapproved diff here would just push a no-op
        # and waste a tick.
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
            agent_approved_sha="stale-approved-sha",
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

        # Bounced directly to validating after the pushed drift resume.
        self.assertIn((80, "validating"), gh.label_history)
        # And NOT through documenting -- docs run after reviewer
        # approval before `in_review`, not on the drift exit.
        self.assertNotIn((80, "documenting"), gh.label_history)
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
        # Stale agent approval cleared so AUTO_MERGE cannot land before
        # the reviewer re-snapshots against the updated body.
        self.assertIsNone(data.get("agent_approved_sha"))

    def test_body_drift_ack_bounces_directly_to_validating(self) -> None:
        # A drift ACK reply (no commit, explicit `ACK:` marker) is an
        # acknowledgement that the existing work already satisfies the
        # edit. The issue bounces DIRECTLY back to `validating` (same
        # destination as the pushed-fix exit; docs do not run on the
        # drift exit, the single docs pass runs after reviewer approval
        # before `in_review` via the final-docs handoff). The other
        # ACK guarantees still hold: `agent_approved_sha` is cleared
        # (the snapshot was for the old requirements, so AUTO_MERGE
        # cannot land the PR until the reviewer re-snapshots) and
        # `review_round` is reset so the reviewer round cap counts
        # fresh rounds.
        gh = FakeGitHubClient()
        issue = make_issue(81, label="in_review", body="new acceptance")
        gh.add_issue(issue)
        pr = FakePR(number=801, head_branch="orchestrator/issue-81")
        gh.add_pr(pr)
        gh.seed_state(
            81,
            user_content_hash="stale-hash",
            dev_agent="claude",
            dev_session_id="dev-sess",
            pr_number=pr.number,
            pr_last_comment_id=0,
            pr_last_review_comment_id=0,
            pr_last_review_summary_id=0,
            branch="orchestrator/issue-81",
            agent_approved_sha="stale-approved-sha",
            review_round=2,
        )

        self._run(
            lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
            run_agent=_agent(
                session_id="dev-sess",
                last_message="ACK: prior commits already satisfy the edit.",
            ),
            dirty_files=(),
            push_branch=True,
            # No commit landed -- before/after SHA match.
            head_shas=["same-sha", "same-sha"],
        )

        # Bounced directly to validating (same destination as the
        # pushed-fix exit; docs do not run on the drift exit, the
        # single docs pass runs after reviewer approval before
        # `in_review`).
        self.assertIn((81, "validating"), gh.label_history)
        self.assertNotIn((81, "documenting"), gh.label_history)
        data = gh.pinned_data(81)
        # `agent_approved_sha` cleared so AUTO_MERGE cannot land before
        # the reviewer re-snapshots against the updated body.
        self.assertIsNone(data.get("agent_approved_sha"))
        # `review_round` reset so the reviewer round cap counts fresh.
        self.assertEqual(data.get("review_round"), 0)
        # ACK was surfaced as an FYI on the issue thread (matches the
        # `_post_user_content_change_result` ack branch).
        self.assertTrue(any(
            "existing work satisfies" in body
            for _, body in gh.posted_comments
        ))

    def test_body_drift_park_does_not_relabel(self) -> None:
        # On a parked outcome (timeout / dirty / push fail / no-commit
        # without ACK) the handler must NOT flip to validating OR
        # documenting -- the dev fix didn't land and the issue stays
        # in `in_review` awaiting human. Preserves the failure-path
        # contract while the success / ACK paths both bounce directly
        # back to `validating`.
        gh = FakeGitHubClient()
        issue = make_issue(82, label="in_review", body="new acceptance")
        gh.add_issue(issue)
        pr = FakePR(number=802, head_branch="orchestrator/issue-82")
        gh.add_pr(pr)
        gh.seed_state(
            82,
            user_content_hash="stale-hash",
            dev_agent="claude",
            dev_session_id="dev-sess",
            pr_number=pr.number,
            pr_last_comment_id=0,
            pr_last_review_comment_id=0,
            pr_last_review_summary_id=0,
            branch="orchestrator/issue-82",
            agent_approved_sha="stale-approved-sha",
        )

        self._run(
            lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
            run_agent=_agent(timed_out=True),
            head_shas=["before"],
        )

        # Did NOT advance into documenting / validating; awaiting human
        # in `in_review`.
        self.assertNotIn((82, "documenting"), gh.label_history)
        self.assertNotIn((82, "validating"), gh.label_history)
        data = gh.pinned_data(82)
        self.assertTrue(data.get("awaiting_human"))


class InReviewFreshFeedbackRouteCoversBothSurfacesTest(
    unittest.TestCase, _PatchedWorkflowMixin,
):
    """Issue-thread and PR-conversation comments share the IssueComment id
    space. The fresh-feedback scan must surface both before the drift
    check runs, otherwise the drift path's `user_content_hash` (which
    only sees the issue thread) would catch the issue-thread comment and
    forward it through the dev-resume path, leaving the PR-conversation
    comment for a later bump to silently consume. By scanning both
    surfaces together and bookmarking the max id across them, the
    fixing route preserves both comments for the (future real) fix
    handler."""

    def test_concurrent_issue_thread_and_pr_conv_both_bookmarked(
        self,
    ) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(
            1300, label="in_review", body="updated body",
        )
        # Issue-thread comment with id 200.
        issue.comments.append(FakeComment(
            id=200, body="adds an acceptance criterion",
            user=FakeUser("alice"),
        ))
        gh.add_issue(issue)
        pr = FakePR(number=13000, head_branch="orchestrator/issue-1300")
        # Concurrent PR-conversation comment at id 150 (between the
        # prior watermark and the issue-thread max).
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
            run_agent=_agent(),
        )

        # Fresh feedback wins over the drift check: the dev is NOT
        # spawned by `_handle_in_review`; the issue routes to `fixing`
        # with a bookmark covering BOTH surfaces (max across the
        # IssueComment id space).
        mocks["run_agent"].assert_not_called()
        self.assertIn((1300, "fixing"), gh.label_history)
        data = gh.pinned_data(1300)
        self.assertEqual(data.get("pending_fix_issue_max_id"), 200)
        # Watermark stays at the seeded value so the future real fix
        # handler can re-scan both surfaces from there and find both
        # comments.
        self.assertEqual(data.get("pr_last_comment_id"), 100)

    def test_pr_conv_comment_above_issue_max_also_bookmarked(
        self,
    ) -> None:
        # Symmetric guard: a PR-conversation comment whose id is HIGHER
        # than every issue-thread id is still picked up by the
        # fresh-feedback scan (it surfaces in `pr_conversation_comments_after`
        # past the IssueComment-space watermark).
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
            run_agent=_agent(),
        )

        mocks["run_agent"].assert_not_called()
        self.assertIn((1310, "fixing"), gh.label_history)
        data = gh.pinned_data(1310)
        self.assertEqual(data.get("pending_fix_issue_max_id"), 600)
