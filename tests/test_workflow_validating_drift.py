# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import os
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("ORCHESTRATOR_SKIP_DOTENV", "1")

from orchestrator import config, workflow

from tests.fakes import (
    FakeComment,
    FakeGitHubClient,
    FakePR,
    FakeUser,
    make_issue,
)
from tests.workflow_helpers import (
    _PatchedWorkflowMixin,
    _TEST_SPEC,
    _agent,
)


class ValidatingTransientParkRecoveryTest(
    unittest.TestCase, _PatchedWorkflowMixin
):
    """A validating-side park whose underlying condition can self-resolve
    (a non-fast-forward push that the next --force-with-lease push will
    land) must auto-recover without needing a fresh issue-thread comment.
    Otherwise `_resume_developer_on_human_reply` -- which only fires on a
    new comment -- leaves the issue parked indefinitely even after the
    transient cause is gone.
    """

    BRANCH = "orchestrator/geserdugarov__agent-orchestrator/issue-170"

    def _parked_issue(self, *, park_reason: str, **extra_state):
        gh = FakeGitHubClient()
        # `last_action_comment_id` is well above any existing comment id, so
        # `comments_after` returns []. This mirrors the post-park watermark
        # set by `_park_awaiting_human` (it bumps to the latest comment id).
        issue = make_issue(170, label="validating")
        gh.add_issue(issue)
        seed = dict(
            pr_number=99, branch=self.BRANCH,
            dev_agent="claude", dev_session_id="dev-sess",
            review_round=1,
            awaiting_human=True,
            park_reason=park_reason,
            last_action_comment_id=10_000,
        )
        seed.update(extra_state)
        gh.seed_state(170, **seed)
        return gh, issue

    def test_push_failed_park_recovers_when_push_succeeds(self) -> None:
        gh, issue = self._parked_issue(park_reason="push_failed")

        # Force the worktree-existence check to pass; "/tmp" always exists
        # on Linux. The recovery only retries the push when the worktree
        # is still on disk (otherwise the dev's local commits are gone and
        # only a human relabel can unstick the issue).
        with patch.object(workflow, "_worktree_path", return_value=Path("/tmp")):
            mocks = self._run(
                lambda: workflow._handle_validating(gh, _TEST_SPEC, issue),
                run_agent=_agent(),
                push_branch=True,
            )

        # Recovery must NOT spawn the agent or post any comment -- it is a
        # silent retry.
        mocks["run_agent"].assert_not_called()
        self.assertEqual(gh.posted_comments, [])
        self.assertEqual(gh.posted_pr_comments, [])
        # Push retried and succeeded: park flags cleared, review_round
        # incremented so the next reviewer run starts a fresh round.
        mocks["_push_branch"].assert_called_once()
        data = gh.pinned_data(170)
        self.assertFalse(data.get("awaiting_human"))
        self.assertIsNone(data.get("park_reason"))
        self.assertEqual(data.get("review_round"), 2)
        # Stays on `validating` (no documenting hop) so the reviewer
        # re-evaluates the recovered head on the next tick.
        self.assertEqual(gh.label_history, [])
        self.assertNotIn((170, "documenting"), gh.label_history)
        self.assertNotIn((170, "in_review"), gh.label_history)

    def test_push_failed_park_stays_parked_when_push_still_fails(self) -> None:
        # Recovery must not re-post the park message when the push still
        # fails -- otherwise every poll would spam the issue.
        gh, issue = self._parked_issue(park_reason="push_failed")

        with patch.object(workflow, "_worktree_path", return_value=Path("/tmp")):
            mocks = self._run(
                lambda: workflow._handle_validating(gh, _TEST_SPEC, issue),
                run_agent=_agent(),
                push_branch=False,
            )

        mocks["run_agent"].assert_not_called()
        mocks["_push_branch"].assert_called_once()
        # No new park comment posted on this tick.
        self.assertEqual(gh.posted_comments, [])
        # Park flags preserved for the next recovery attempt.
        data = gh.pinned_data(170)
        self.assertTrue(data.get("awaiting_human"))
        self.assertEqual(data.get("park_reason"), "push_failed")
        # review_round NOT bumped while still stuck.
        self.assertEqual(data.get("review_round"), 1)

    def test_push_failed_park_stays_parked_when_worktree_is_gone(self) -> None:
        # If the worktree was reaped between the original park and the
        # recovery tick, the dev's local commits are gone and there is
        # nothing to push. Stay parked so a human can intervene.
        gh, issue = self._parked_issue(park_reason="push_failed")

        # Path that will not exist on the test host.
        gone = Path("/tmp/orchestrator-test-recovery-no-such-worktree-xyz")
        with patch.object(workflow, "_worktree_path", return_value=gone):
            mocks = self._run(
                lambda: workflow._handle_validating(gh, _TEST_SPEC, issue),
                run_agent=_agent(),
                push_branch=True,
            )

        mocks["run_agent"].assert_not_called()
        mocks["_push_branch"].assert_not_called()
        data = gh.pinned_data(170)
        self.assertTrue(data.get("awaiting_human"))
        self.assertEqual(data.get("park_reason"), "push_failed")

    def test_non_transient_park_stays_parked_with_no_new_comments(self) -> None:
        # A park whose reason is not in the validating transient set (e.g.
        # a question or dirty-tree park) must NOT auto-recover. The
        # _resume_developer_on_human_reply path (no new comments) returns
        # without doing anything; recovery is the only other path and it
        # bails on park_reason.
        gh, issue = self._parked_issue(park_reason=None)

        with patch.object(workflow, "_worktree_path", return_value=Path("/tmp")):
            mocks = self._run(
                lambda: workflow._handle_validating(gh, _TEST_SPEC, issue),
                run_agent=_agent(),
                push_branch=True,
            )

        mocks["run_agent"].assert_not_called()
        mocks["_push_branch"].assert_not_called()
        data = gh.pinned_data(170)
        self.assertTrue(data.get("awaiting_human"))
        self.assertEqual(data.get("review_round"), 1)

    def test_reviewer_timeout_park_recovers_silently(self) -> None:
        # A previous tick parked because the reviewer agent timed out.
        # The next tick must clear the flags so the reviewer re-runs --
        # nothing in `_resume_developer_on_human_reply` would unstick this
        # otherwise (no comment ever lands from a timeout).
        gh, issue = self._parked_issue(park_reason="reviewer_timeout")

        with patch.object(workflow, "_worktree_path", return_value=Path("/tmp")):
            mocks = self._run(
                lambda: workflow._handle_validating(gh, _TEST_SPEC, issue),
                run_agent=_agent(),
                push_branch=True,
            )

        # Recovery is silent on this tick: the agent is NOT re-spawned
        # here (next tick does that, on the cleared awaiting_human flag),
        # no push is attempted (no fix landed), and no new comment is
        # posted.
        mocks["run_agent"].assert_not_called()
        mocks["_push_branch"].assert_not_called()
        self.assertEqual(gh.posted_comments, [])
        data = gh.pinned_data(170)
        self.assertFalse(data.get("awaiting_human"))
        self.assertIsNone(data.get("park_reason"))
        # review_round MUST NOT advance: a timeout produced no fix, so
        # bumping would burn through MAX_REVIEW_ROUNDS without progress.
        self.assertEqual(data.get("review_round"), 1)

    def test_reviewer_failed_park_recovers_silently(self) -> None:
        # The reviewer crashed with empty stdout + non-zero exit on the
        # previous tick. Recovery must clear the flags so the next tick
        # re-spawns the reviewer with a fresh budget -- without this,
        # the issue waits for a human comment that the codex / network
        # blip cannot produce.
        gh, issue = self._parked_issue(park_reason="reviewer_failed")

        with patch.object(workflow, "_worktree_path", return_value=Path("/tmp")):
            mocks = self._run(
                lambda: workflow._handle_validating(gh, _TEST_SPEC, issue),
                run_agent=_agent(),
                push_branch=True,
            )

        mocks["run_agent"].assert_not_called()
        mocks["_push_branch"].assert_not_called()
        self.assertEqual(gh.posted_comments, [])
        data = gh.pinned_data(170)
        self.assertFalse(data.get("awaiting_human"))
        self.assertIsNone(data.get("park_reason"))
        # No fix landed; a reviewer crash produces no commit, so the
        # round must stay flat (mirrors the reviewer_timeout branch).
        self.assertEqual(data.get("review_round"), 1)

    def test_reviewer_failed_park_with_new_comment_routes_to_reviewer(self) -> None:
        # A human "Retry" / "Continue" nudge after a reviewer-side park
        # must wake the REVIEWER, not the dev. Pre-fix this branch fed
        # the comment to `_resume_developer_on_human_reply`, which woke
        # the dev session; the dev correctly answered "nothing to do,
        # the reviewer should re-run" and the issue wedged.
        gh, issue = self._parked_issue(park_reason="reviewer_failed")
        issue.comments.append(
            FakeComment(
                id=10_500, body="retry please",
                user=FakeUser("alice"),
            )
        )

        review = _agent(
            session_id="rev-sess",
            last_message="LGTM\n\nVERDICT: APPROVED",
        )
        with patch.object(workflow, "_worktree_path", return_value=Path("/tmp")):
            mocks = self._run(
                lambda: workflow._handle_validating(gh, _TEST_SPEC, issue),
                run_agent=review,
                head_shas=["cafe1234"],
            )

        # Exactly one agent ran: the reviewer (not the dev). The agent
        # call must use the reviewer config, not the dev session resume.
        self.assertEqual(mocks["run_agent"].call_count, 1)
        call = mocks["run_agent"].call_args
        self.assertEqual(call.args[0], config.REVIEW_AGENT)
        self.assertNotIn("resume_session_id", call.kwargs)
        # Park flags cleared and the human's comment is consumed so it
        # cannot replay on the next tick.
        data = gh.pinned_data(170)
        self.assertFalse(data.get("awaiting_human"))
        self.assertIsNone(data.get("park_reason"))
        self.assertEqual(data.get("last_action_comment_id"), 10_500)

    def test_reviewer_timeout_park_with_new_comment_routes_to_reviewer(self) -> None:
        # Same routing rule for the reviewer_timeout park reason: a
        # human nudge must reach the reviewer, not the dev session.
        gh, issue = self._parked_issue(park_reason="reviewer_timeout")
        issue.comments.append(
            FakeComment(
                id=10_500, body="retry please",
                user=FakeUser("alice"),
            )
        )

        review = _agent(
            session_id="rev-sess",
            last_message="LGTM\n\nVERDICT: APPROVED",
        )
        with patch.object(workflow, "_worktree_path", return_value=Path("/tmp")):
            mocks = self._run(
                lambda: workflow._handle_validating(gh, _TEST_SPEC, issue),
                run_agent=review,
                head_shas=["cafe1234"],
            )

        self.assertEqual(mocks["run_agent"].call_count, 1)
        call = mocks["run_agent"].call_args
        self.assertEqual(call.args[0], config.REVIEW_AGENT)
        self.assertNotIn("resume_session_id", call.kwargs)
        data = gh.pinned_data(170)
        self.assertFalse(data.get("awaiting_human"))
        self.assertIsNone(data.get("park_reason"))

    def test_agent_timeout_park_with_new_comment_still_routes_to_dev(self) -> None:
        # Regression: dev-side park reasons (agent_timeout) must keep
        # routing to the dev session on a human comment. Only
        # reviewer-side reasons get the new fall-through.
        gh, issue = self._parked_issue(
            park_reason="agent_timeout",
            pre_dev_fix_sha="cafe1234",
        )
        issue.comments.append(
            FakeComment(
                id=10_500, body="please rebase first",
                user=FakeUser("alice"),
            )
        )

        with patch.object(workflow, "_worktree_path", return_value=Path("/tmp")):
            mocks = self._run(
                lambda: workflow._handle_validating(gh, _TEST_SPEC, issue),
                run_agent=_agent(
                    session_id="dev-sess", last_message="rebased",
                ),
                push_branch=True,
                head_shas=["aaa", "bbb"],
            )

        # The dev was resumed with the human's feedback (NOT the reviewer).
        mocks["run_agent"].assert_called_once()
        call = mocks["run_agent"].call_args
        self.assertEqual(call.kwargs.get("resume_session_id"), "dev-sess")
        followup = call.args[1]
        self.assertIn("please rebase first", followup)

    def test_agent_timeout_clean_tree_no_commits_recovers_silently(self) -> None:
        # Common timeout shape: the dev burned the budget without
        # producing a new commit. Recovery clears flags and does not
        # bump the round (no fix landed); next tick re-runs the reviewer.
        # `head_shas[0] == pre_dev_fix_sha` models "agent did nothing"
        # (worktree HEAD unchanged from the pre-agent watermark).
        gh, issue = self._parked_issue(
            park_reason="agent_timeout",
            pre_dev_fix_sha="cafe1234",
        )

        with patch.object(workflow, "_worktree_path", return_value=Path("/tmp")):
            mocks = self._run(
                lambda: workflow._handle_validating(gh, _TEST_SPEC, issue),
                run_agent=_agent(),
                dirty_files=(),
                push_branch=True,
                head_shas=("cafe1234",),
            )

        mocks["run_agent"].assert_not_called()
        mocks["_push_branch"].assert_not_called()
        self.assertEqual(gh.posted_comments, [])
        data = gh.pinned_data(170)
        self.assertFalse(data.get("awaiting_human"))
        self.assertIsNone(data.get("park_reason"))
        self.assertEqual(data.get("review_round"), 1)
        # Watermark cleared so a future timeout cycle starts fresh.
        self.assertIsNone(data.get("pre_dev_fix_sha"))

    def test_agent_timeout_existing_pr_commits_no_new_commit(self) -> None:
        # Regression: a normal PR worktree is always ahead of
        # `origin/<base>` after the first fix lands. `_has_new_commits()`
        # would say "yes" even when this run produced nothing, so naive
        # recovery would call `_push_branch()` (force-with-lease over
        # the live remote head with a stale local HEAD) and bump the
        # round on every tick. The pre/now SHA comparison must guard
        # against that.
        gh, issue = self._parked_issue(
            park_reason="agent_timeout",
            pre_dev_fix_sha="cafe1234",
        )

        with patch.object(workflow, "_worktree_path", return_value=Path("/tmp")):
            mocks = self._run(
                lambda: workflow._handle_validating(gh, _TEST_SPEC, issue),
                run_agent=_agent(),
                # Mock `_has_new_commits` to True to model an established
                # PR worktree (commits ahead of origin/main); the
                # recovery must not consult this signal.
                has_new_commits=True,
                dirty_files=(),
                push_branch=True,
                head_shas=("cafe1234",),  # HEAD == pre_dev_fix_sha
            )

        mocks["run_agent"].assert_not_called()
        mocks["_push_branch"].assert_not_called()
        self.assertEqual(gh.posted_comments, [])
        data = gh.pinned_data(170)
        self.assertFalse(data.get("awaiting_human"))
        self.assertIsNone(data.get("park_reason"))
        # MUST NOT bump: nothing landed.
        self.assertEqual(data.get("review_round"), 1)

    def test_agent_timeout_with_unpushed_commits_pushes_and_bumps(self) -> None:
        # The dev committed the fix locally but the timeout killed it
        # before the push. Recovery must finish that push -- otherwise
        # the next tick's reviewer would inspect a SHA that is not on
        # the PR. `head_shas[0] != pre_dev_fix_sha` models "agent
        # produced a new commit before timing out."
        gh, issue = self._parked_issue(
            park_reason="agent_timeout",
            pre_dev_fix_sha="cafe1234",
        )

        with patch.object(workflow, "_worktree_path", return_value=Path("/tmp")):
            mocks = self._run(
                lambda: workflow._handle_validating(gh, _TEST_SPEC, issue),
                run_agent=_agent(),
                dirty_files=(),
                push_branch=True,
                head_shas=("beef5678",),  # HEAD moved past pre-agent SHA
            )

        mocks["run_agent"].assert_not_called()
        mocks["_push_branch"].assert_called_once()
        self.assertEqual(gh.posted_comments, [])
        data = gh.pinned_data(170)
        self.assertFalse(data.get("awaiting_human"))
        self.assertIsNone(data.get("park_reason"))
        # Bumped: a real fix landed.
        self.assertEqual(data.get("review_round"), 2)
        self.assertIsNone(data.get("pre_dev_fix_sha"))
        # Stays on `validating` (no documenting hop) so the reviewer
        # re-evaluates the recovered head on the next tick.
        self.assertNotIn((170, "documenting"), gh.label_history)

    def test_agent_timeout_with_unpushed_commits_push_fails_stays_parked(
        self,
    ) -> None:
        gh, issue = self._parked_issue(
            park_reason="agent_timeout",
            pre_dev_fix_sha="cafe1234",
        )

        with patch.object(workflow, "_worktree_path", return_value=Path("/tmp")):
            mocks = self._run(
                lambda: workflow._handle_validating(gh, _TEST_SPEC, issue),
                run_agent=_agent(),
                dirty_files=(),
                push_branch=False,
                head_shas=("beef5678",),
            )

        mocks["_push_branch"].assert_called_once()
        self.assertEqual(gh.posted_comments, [])
        data = gh.pinned_data(170)
        self.assertTrue(data.get("awaiting_human"))
        self.assertEqual(data.get("park_reason"), "agent_timeout")
        # NOT bumped while still stuck; watermark preserved for next try.
        self.assertEqual(data.get("review_round"), 1)
        self.assertEqual(data.get("pre_dev_fix_sha"), "cafe1234")

    def test_agent_timeout_with_dirty_worktree_stays_parked(self) -> None:
        # The dev edited files without committing before timing out.
        # Recovery refuses to silently push (would publish an incomplete
        # branch) or to clear flags (the next reviewer would inspect
        # uncommitted state). Stays parked until a human or comment-
        # driven resume sorts the dirty edits out.
        gh, issue = self._parked_issue(
            park_reason="agent_timeout",
            pre_dev_fix_sha="cafe1234",
        )

        with patch.object(workflow, "_worktree_path", return_value=Path("/tmp")):
            mocks = self._run(
                lambda: workflow._handle_validating(gh, _TEST_SPEC, issue),
                run_agent=_agent(),
                dirty_files=["leftover.py"],
                push_branch=True,
            )

        mocks["run_agent"].assert_not_called()
        mocks["_push_branch"].assert_not_called()
        # No new comment posted on this tick -- the original park
        # message still describes the situation.
        self.assertEqual(gh.posted_comments, [])
        data = gh.pinned_data(170)
        self.assertTrue(data.get("awaiting_human"))
        self.assertEqual(data.get("park_reason"), "agent_timeout")
        self.assertEqual(data.get("review_round"), 1)

    def test_agent_timeout_without_watermark_stays_parked(self) -> None:
        # Defensive: if the timeout park ran in foreign code that did
        # not persist `pre_dev_fix_sha`, recovery cannot tell whether a
        # commit was produced. Refuse to act -- a force-push of a stale
        # local HEAD would silently rewrite remote.
        gh, issue = self._parked_issue(park_reason="agent_timeout")

        with patch.object(workflow, "_worktree_path", return_value=Path("/tmp")):
            mocks = self._run(
                lambda: workflow._handle_validating(gh, _TEST_SPEC, issue),
                run_agent=_agent(),
                dirty_files=(),
                push_branch=True,
                head_shas=("anything",),
            )

        mocks["run_agent"].assert_not_called()
        mocks["_push_branch"].assert_not_called()
        data = gh.pinned_data(170)
        self.assertTrue(data.get("awaiting_human"))
        self.assertEqual(data.get("park_reason"), "agent_timeout")

    def test_transient_park_with_new_comment_takes_resume_path(self) -> None:
        # A transient park is preempted by a fresh human comment: the
        # comment-driven resume path wins, the dev is spawned with the
        # human's feedback, and the recovery branch does not silently
        # retry the push. This ensures the human's reply is not dropped.
        gh, issue = self._parked_issue(park_reason="push_failed")
        issue.comments.append(
            FakeComment(
                id=10_500, body="please rebase first",
                user=FakeUser("alice"),
            )
        )

        with patch.object(workflow, "_worktree_path", return_value=Path("/tmp")):
            mocks = self._run(
                lambda: workflow._handle_validating(gh, _TEST_SPEC, issue),
                run_agent=_agent(
                    session_id="dev-sess", last_message="rebased",
                ),
                push_branch=True,
                head_shas=["aaa", "bbb"],
            )

        # Dev was resumed with the human's feedback (recovery did NOT run).
        mocks["run_agent"].assert_called_once()
        followup = mocks["run_agent"].call_args.args[1]
        self.assertIn("please rebase first", followup)
        data = gh.pinned_data(170)
        self.assertFalse(data.get("awaiting_human"))


class HandleValidatingResumeOnHashChangeTest(
    unittest.TestCase, _PatchedWorkflowMixin,
):
    def test_body_drift_resumes_dev_and_stays_on_validating(self) -> None:
        # While validating (PR is open), a human edit must not discard the
        # dev's already-pushed work. Notify and resume; on a successful
        # pushed fix, stay on `validating` so the reviewer re-evaluates
        # the new diff next tick. The docs pass only runs as the
        # final-docs handoff after a fresh approval.
        gh = FakeGitHubClient()
        issue = make_issue(70, label="validating", body="updated criteria")
        gh.add_issue(issue)
        pr = FakePR(number=700, head_branch="orchestrator/geserdugarov__agent-orchestrator/issue-70")
        gh.add_pr(pr)
        gh.seed_state(
            70,
            user_content_hash="stale-hash",
            dev_agent="claude",
            dev_session_id="dev-sess",
            pr_number=pr.number,
            review_round=0,
            branch="orchestrator/geserdugarov__agent-orchestrator/issue-70",
        )

        self._run(
            lambda: workflow._handle_validating(gh, _TEST_SPEC, issue),
            run_agent=_agent(
                session_id="dev-sess", last_message="fixed"
            ),
            has_new_commits=True,
            dirty_files=(),
            push_branch=True,
            head_shas=["before-sha", "after-sha"],
        )

        # Stays on `validating`: no documenting hop, and the reviewer
        # has NOT been spawned this tick (the only run_agent call was
        # the dev resume).
        self.assertNotIn((70, "documenting"), gh.label_history)
        self.assertNotIn((70, "in_review"), gh.label_history)
        # Notice posted on the issue thread.
        self.assertTrue(any(
            "issue body changed" in body
            for _, body in gh.posted_comments
        ))
        # review_round incremented so the validating cap stays accurate.
        data = gh.pinned_data(70)
        self.assertEqual(data.get("review_round"), 1)


class ValidatingDriftDefersToReviewerRecoveryTest(
    unittest.TestCase, _PatchedWorkflowMixin,
):
    """Reviewer point 1: when validating is parked with a reviewer-side
    park reason (`reviewer_timeout` / `reviewer_failed`), a human "retry"
    comment must re-spawn the REVIEWER, not the dev session. The drift
    check fires first because the human's comment also flips the hash;
    the drift handler must defer to the awaiting-human branch in this
    case so the reviewer re-runs naturally."""

    def test_reviewer_timeout_drift_respawns_reviewer_not_dev(
        self,
    ) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(
            1000, label="validating", body="initial body",
        )
        # Pre-existing human "retry" comment that triggers the drift
        # detection (the hash includes non-orchestrator comments).
        human = FakeComment(
            id=4000, body="retry the reviewer please",
            user=FakeUser("alice"),
        )
        issue.comments.append(human)
        gh.add_issue(issue)
        pr = FakePR(number=10000, head_branch="orchestrator/geserdugarov__agent-orchestrator/issue-1000")
        gh.add_pr(pr)
        # Pre-seed a real `user_content_hash` (the bug surfaces only
        # when the hash is already set; first-tick auto-seeding hides it).
        seed_hash = workflow._compute_user_content_hash(
            make_issue(1000, body="initial body"), set(),
        )
        gh.seed_state(
            1000,
            pr_number=pr.number,
            dev_agent="claude",
            dev_session_id="dev-sess",
            review_round=1,
            branch="orchestrator/geserdugarov__agent-orchestrator/issue-1000",
            awaiting_human=True,
            park_reason="reviewer_timeout",
            last_action_comment_id=100,
            user_content_hash=seed_hash,
        )

        mocks = self._run(
            lambda: workflow._handle_validating(gh, _TEST_SPEC, issue),
            run_agent=_agent(
                session_id="rev-sess",
                last_message="Looks fine.\n\nVERDICT: APPROVED",
            ),
            has_new_commits=False,
            head_shas=["head"],
        )

        # The reviewer (REVIEW_AGENT) ran, NOT the dev session. The
        # agent invocation should have been against the review agent
        # binary, with a review-style prompt.
        call_args = mocks["run_agent"].call_args
        self.assertEqual(call_args[0][0], config.REVIEW_AGENT)
        self.assertIn("automated code reviewer", call_args[0][1])
        # No drift-style ":pencil2: issue body changed; resuming dev
        # session" notice was posted -- the drift was deferred.
        self.assertFalse(any(
            ":pencil2:" in body and "resuming dev session" in body
            for _, body in gh.posted_comments
        ))
        # The reviewer recovery consumed the human comment and cleared
        # the park flags.
        data = gh.pinned_data(1000)
        self.assertFalse(data.get("awaiting_human"))
        self.assertIsNone(data.get("park_reason"))
        # The new hash baseline was persisted so the next tick doesn't
        # loop on the same drift.
        new_hash = workflow._compute_user_content_hash(issue, set())
        self.assertEqual(data.get("user_content_hash"), new_hash)
