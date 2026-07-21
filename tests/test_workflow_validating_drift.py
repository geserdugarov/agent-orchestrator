# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

from orchestrator import config, workflow

from tests.fakes import (
    FakeComment,
    FakeGitHubClient,
    FakePR,
    FakeUser,
    make_issue,
)
from tests.workflow_helpers import (
    REVIEW_APPROVED_MESSAGE,
    _PatchedWorkflowMixin,
    _agent,
)

VALIDATING_ISSUE = 170
VALIDATING_PR = 99
VALIDATING_BRANCH = "orchestrator/geserdugarov__agent-orchestrator/issue-170"
BODY_DRIFT_ISSUE = 70
BODY_DRIFT_PR = 700
REVIEWER_RETRY_COMMENT_ID = 4000
REVIEWER_DRIFT_PR = 10000
ACTION_WATERMARK = 10_000
HUMAN_REPLY_ID = 10_500
DEV_SESSION = "dev-sess"
HUMAN_LOGIN = "alice"
PRE_FIX_SHA = "cafe1234"
PUSH_FAILED = "push_failed"
AGENT_TIMEOUT = "agent_timeout"
REBASE_REQUEST = "please rebase first"
WORKTREE_ROOT = "/tmp"
WORKTREE_PATH = "_worktree_path"
RUN_AGENT = "run_agent"
PUSH_BRANCH = "_push_branch"
AWAITING_HUMAN = "awaiting_human"
PARK_REASON = "park_reason"
REVIEW_ROUND = "review_round"


class _TransientParkFixtureMixin(_PatchedWorkflowMixin):
    def _parked_issue(self, *, park_reason: str, **extra_state):
        gh = FakeGitHubClient()
        # `last_action_comment_id` is well above any existing comment id, so
        # `comments_after` returns []. This mirrors the post-park watermark
        # set by `_park_awaiting_human` (it bumps to the latest comment id).
        issue = make_issue(VALIDATING_ISSUE, label="validating")
        gh.add_issue(issue)
        seed = dict(
            pr_number=VALIDATING_PR,
            branch=VALIDATING_BRANCH,
            dev_agent="claude",
            dev_session_id=DEV_SESSION,
            review_round=1,
            awaiting_human=True,
            park_reason=park_reason,
            last_action_comment_id=ACTION_WATERMARK,
        )
        seed.update(extra_state)
        gh.seed_state(VALIDATING_ISSUE, **seed)
        return gh, issue


class ValidatingTransientParkRecoveryTest(
    unittest.TestCase,
    _TransientParkFixtureMixin,
):
    """Recover safe push failures while retaining ambiguous parks."""

    def test_push_failure_recovers_on_success(self) -> None:
        gh, issue = self._parked_issue(park_reason=PUSH_FAILED)

        # Force the worktree-existence check to pass; "/tmp" always exists
        # on Linux. The recovery only retries the push when the worktree
        # is still on disk (otherwise the dev's local commits are gone and
        # only a human relabel can unstick the issue).
        with patch.object(workflow, WORKTREE_PATH, return_value=Path(WORKTREE_ROOT)):
            mocks = self._run_validating(
                gh,
                issue,
                run_agent=_agent(),
                push_branch=True,
            )

        # Recovery must NOT spawn the agent or post any comment -- it is a
        # silent retry.
        mocks[RUN_AGENT].assert_not_called()
        self.assertEqual(gh.posted_comments, [])
        self.assertEqual(gh.posted_pr_comments, [])
        # Push retried and succeeded: park flags cleared, review_round
        # incremented so the next reviewer run starts a fresh round.
        mocks[PUSH_BRANCH].assert_called_once()
        state = gh.pinned_data(VALIDATING_ISSUE)
        self.assertFalse(state.get(AWAITING_HUMAN))
        self.assertIsNone(state.get(PARK_REASON))
        self.assertEqual(state.get(REVIEW_ROUND), 2)
        # Stays on `validating` (no documenting hop) so the reviewer
        # re-evaluates the recovered head on the next tick.
        self.assertEqual(gh.label_history, [])
        self.assertNotIn((VALIDATING_ISSUE, "documenting"), gh.label_history)
        self.assertNotIn((VALIDATING_ISSUE, "in_review"), gh.label_history)

    def test_repeat_push_failure_stays_parked(self) -> None:
        # Recovery must not re-post the park message when the push still
        # fails -- otherwise every poll would spam the issue.
        gh, issue = self._parked_issue(park_reason=PUSH_FAILED)

        with patch.object(workflow, WORKTREE_PATH, return_value=Path(WORKTREE_ROOT)):
            mocks = self._run_validating(
                gh,
                issue,
                run_agent=_agent(),
                push_branch=False,
            )

        mocks[RUN_AGENT].assert_not_called()
        mocks[PUSH_BRANCH].assert_called_once()
        # No new park comment posted on this tick.
        self.assertEqual(gh.posted_comments, [])
        # Park flags preserved for the next recovery attempt.
        state = gh.pinned_data(VALIDATING_ISSUE)
        self.assertTrue(state.get(AWAITING_HUMAN))
        self.assertEqual(state.get(PARK_REASON), PUSH_FAILED)
        # review_round NOT bumped while still stuck.
        self.assertEqual(state.get(REVIEW_ROUND), 1)

    def test_missing_worktree_stays_parked(self) -> None:
        # If the worktree was reaped between the original park and the
        # recovery tick, the dev's local commits are gone and there is
        # nothing to push. Stay parked so a human can intervene.
        gh, issue = self._parked_issue(park_reason=PUSH_FAILED)

        # Path that will not exist on the test host.
        gone = Path("/tmp/orchestrator-test-recovery-no-such-worktree-xyz")
        with patch.object(workflow, WORKTREE_PATH, return_value=gone):
            mocks = self._run_validating(
                gh,
                issue,
                run_agent=_agent(),
                push_branch=True,
            )

        mocks[RUN_AGENT].assert_not_called()
        mocks[PUSH_BRANCH].assert_not_called()
        state = gh.pinned_data(VALIDATING_ISSUE)
        self.assertTrue(state.get(AWAITING_HUMAN))
        self.assertEqual(state.get(PARK_REASON), PUSH_FAILED)

    def test_nontransient_no_comments_stays_parked(self) -> None:
        # A park whose reason is not in the validating transient set (e.g.
        # a question or dirty-tree park) must NOT auto-recover. The
        # _resume_developer_on_human_reply path (no new comments) returns
        # without doing anything; recovery is the only other path and it
        # bails on park_reason.
        gh, issue = self._parked_issue(park_reason=None)

        with patch.object(workflow, WORKTREE_PATH, return_value=Path(WORKTREE_ROOT)):
            mocks = self._run_validating(
                gh,
                issue,
                run_agent=_agent(),
                push_branch=True,
            )

        mocks[RUN_AGENT].assert_not_called()
        mocks[PUSH_BRANCH].assert_not_called()
        state = gh.pinned_data(VALIDATING_ISSUE)
        self.assertTrue(state.get(AWAITING_HUMAN))
        self.assertEqual(state.get(REVIEW_ROUND), 1)


class ValidatingReviewerParkRecoveryTest(
    unittest.TestCase,
    _TransientParkFixtureMixin,
):
    """Recover reviewer-side transient parks or rerun on a human reply."""

    def test_reviewer_timeout_park_recovers_silently(self) -> None:
        # A previous tick parked because the reviewer agent timed out.
        # The next tick must clear the flags so the reviewer re-runs --
        # nothing in `_resume_developer_on_human_reply` would unstick this
        # otherwise (no comment ever lands from a timeout).
        gh, issue = self._parked_issue(park_reason="reviewer_timeout")

        with patch.object(workflow, WORKTREE_PATH, return_value=Path(WORKTREE_ROOT)):
            mocks = self._run_validating(
                gh,
                issue,
                run_agent=_agent(),
                push_branch=True,
            )

        # Recovery is silent on this tick: the agent is NOT re-spawned
        # here (next tick does that, on the cleared awaiting_human flag),
        # no push is attempted (no fix landed), and no new comment is
        # posted.
        mocks[RUN_AGENT].assert_not_called()
        mocks[PUSH_BRANCH].assert_not_called()
        self.assertEqual(gh.posted_comments, [])
        state = gh.pinned_data(VALIDATING_ISSUE)
        self.assertFalse(state.get(AWAITING_HUMAN))
        self.assertIsNone(state.get(PARK_REASON))
        # review_round MUST NOT advance: a timeout produced no fix, so
        # bumping would burn through MAX_REVIEW_ROUNDS without progress.
        self.assertEqual(state.get(REVIEW_ROUND), 1)

    def test_reviewer_failed_park_recovers_silently(self) -> None:
        # The reviewer crashed with empty stdout + non-zero exit on the
        # previous tick. Recovery must clear the flags so the next tick
        # re-spawns the reviewer with a fresh budget -- without this,
        # the issue waits for a human comment that the codex / network
        # blip cannot produce.
        gh, issue = self._parked_issue(park_reason="reviewer_failed")

        with patch.object(workflow, WORKTREE_PATH, return_value=Path(WORKTREE_ROOT)):
            mocks = self._run_validating(
                gh,
                issue,
                run_agent=_agent(),
                push_branch=True,
            )

        mocks[RUN_AGENT].assert_not_called()
        mocks[PUSH_BRANCH].assert_not_called()
        self.assertEqual(gh.posted_comments, [])
        state = gh.pinned_data(VALIDATING_ISSUE)
        self.assertFalse(state.get(AWAITING_HUMAN))
        self.assertIsNone(state.get(PARK_REASON))
        # No fix landed; a reviewer crash produces no commit, so the
        # round must stay flat (mirrors the reviewer_timeout branch).
        self.assertEqual(state.get(REVIEW_ROUND), 1)

    def test_error_comment_reruns_reviewer(self) -> None:
        # A human "Retry" / "Continue" nudge after a reviewer-side park
        # must wake the REVIEWER, not the dev. Pre-fix this branch fed
        # the comment to `_resume_developer_on_human_reply`, which woke
        # the dev session; the dev correctly answered "nothing to do,
        # the reviewer should re-run" and the issue wedged.
        gh, issue = self._parked_issue(park_reason="reviewer_failed")
        issue.comments.append(
            FakeComment(
                id=HUMAN_REPLY_ID,
                body="retry please",
                user=FakeUser(HUMAN_LOGIN),
            )
        )

        review = _agent(
            session_id="rev-sess",
            last_message=REVIEW_APPROVED_MESSAGE,
        )
        with patch.object(workflow, WORKTREE_PATH, return_value=Path(WORKTREE_ROOT)):
            mocks = self._run_validating(
                gh,
                issue,
                run_agent=review,
                head_shas=[PRE_FIX_SHA],
            )

        # Exactly one agent ran: the reviewer (not the dev). The agent
        # call must use the reviewer config, not the dev session resume.
        self.assertEqual(mocks[RUN_AGENT].call_count, 1)
        call = mocks[RUN_AGENT].call_args
        self.assertEqual(call.args[0], config.REVIEW_AGENT)
        self.assertNotIn("resume_session_id", call.kwargs)
        # Park flags cleared and the human's comment is consumed so it
        # cannot replay on the next tick.
        state = gh.pinned_data(VALIDATING_ISSUE)
        self.assertFalse(state.get(AWAITING_HUMAN))
        self.assertIsNone(state.get(PARK_REASON))
        self.assertEqual(state.get("last_action_comment_id"), HUMAN_REPLY_ID)

    def test_timeout_comment_reruns_reviewer(self) -> None:
        # Same routing rule for the reviewer_timeout park reason: a
        # human nudge must reach the reviewer, not the dev session.
        gh, issue = self._parked_issue(park_reason="reviewer_timeout")
        issue.comments.append(
            FakeComment(
                id=HUMAN_REPLY_ID,
                body="retry please",
                user=FakeUser(HUMAN_LOGIN),
            )
        )

        review = _agent(
            session_id="rev-sess",
            last_message=REVIEW_APPROVED_MESSAGE,
        )
        with patch.object(workflow, WORKTREE_PATH, return_value=Path(WORKTREE_ROOT)):
            mocks = self._run_validating(
                gh,
                issue,
                run_agent=review,
                head_shas=[PRE_FIX_SHA],
            )

        self.assertEqual(mocks[RUN_AGENT].call_count, 1)
        call = mocks[RUN_AGENT].call_args
        self.assertEqual(call.args[0], config.REVIEW_AGENT)
        self.assertNotIn("resume_session_id", call.kwargs)
        state = gh.pinned_data(VALIDATING_ISSUE)
        self.assertFalse(state.get(AWAITING_HUMAN))
        self.assertIsNone(state.get(PARK_REASON))


class ValidatingDevParkRecoveryTest(
    unittest.TestCase,
    _TransientParkFixtureMixin,
):
    """Recover dev timeouts from the worktree and push state."""

    def test_dev_timeout_comment_routes_to_dev(self) -> None:
        # Regression: dev-side park reasons (agent_timeout) must keep
        # routing to the dev session on a human comment. Only
        # reviewer-side reasons get the new fall-through.
        gh, issue = self._parked_issue(
            park_reason=AGENT_TIMEOUT,
            pre_dev_fix_sha=PRE_FIX_SHA,
        )
        issue.comments.append(
            FakeComment(
                id=HUMAN_REPLY_ID,
                body=REBASE_REQUEST,
                user=FakeUser(HUMAN_LOGIN),
            )
        )

        with patch.object(workflow, WORKTREE_PATH, return_value=Path(WORKTREE_ROOT)):
            mocks = self._run_validating(
                gh,
                issue,
                run_agent=_agent(
                    session_id=DEV_SESSION,
                    last_message="rebased",
                ),
                push_branch=True,
                head_shas=["aaa", "bbb"],
            )

        # The dev was resumed with the human's feedback (NOT the reviewer).
        mocks[RUN_AGENT].assert_called_once()
        call = mocks[RUN_AGENT].call_args
        self.assertEqual(call.kwargs.get("resume_session_id"), DEV_SESSION)
        followup = call.args[1]
        self.assertIn(REBASE_REQUEST, followup)

    def test_clean_timeout_recovers_silently(self) -> None:
        # Common timeout shape: the dev burned the budget without
        # producing a new commit. Recovery clears flags and does not
        # bump the round (no fix landed); next tick re-runs the reviewer.
        # `head_shas[0] == pre_dev_fix_sha` models "agent did nothing"
        # (worktree HEAD unchanged from the pre-agent watermark).
        gh, issue = self._parked_issue(
            park_reason=AGENT_TIMEOUT,
            pre_dev_fix_sha=PRE_FIX_SHA,
        )

        with patch.object(workflow, WORKTREE_PATH, return_value=Path(WORKTREE_ROOT)):
            mocks = self._run_validating(
                gh,
                issue,
                run_agent=_agent(),
                dirty_files=(),
                push_branch=True,
                head_shas=(PRE_FIX_SHA,),
            )

        mocks[RUN_AGENT].assert_not_called()
        mocks[PUSH_BRANCH].assert_not_called()
        self.assertEqual(gh.posted_comments, [])
        state = gh.pinned_data(VALIDATING_ISSUE)
        self.assertFalse(state.get(AWAITING_HUMAN))
        self.assertIsNone(state.get(PARK_REASON))
        self.assertEqual(state.get(REVIEW_ROUND), 1)
        # Watermark cleared so a future timeout cycle starts fresh.
        self.assertIsNone(state.get("pre_dev_fix_sha"))

    def test_timeout_with_only_pr_commits_recovers(self) -> None:
        # Regression: a normal PR worktree is always ahead of
        # `origin/<base>` after the first fix lands. `_has_new_commits()`
        # would say "yes" even when this run produced nothing, so naive
        # recovery would call `_push_branch()` (force-with-lease over
        # the live remote head with a stale local HEAD) and bump the
        # round on every tick. The pre/now SHA comparison must guard
        # against that.
        gh, issue = self._parked_issue(
            park_reason=AGENT_TIMEOUT,
            pre_dev_fix_sha=PRE_FIX_SHA,
        )

        with patch.object(workflow, WORKTREE_PATH, return_value=Path(WORKTREE_ROOT)):
            mocks = self._run_validating(
                gh,
                issue,
                run_agent=_agent(),
                # Mock `_has_new_commits` to True to model an established
                # PR worktree (commits ahead of origin/main); the
                # recovery must not consult this signal.
                has_new_commits=True,
                dirty_files=(),
                push_branch=True,
                head_shas=(PRE_FIX_SHA,),  # HEAD == pre_dev_fix_sha
            )

        mocks[RUN_AGENT].assert_not_called()
        mocks[PUSH_BRANCH].assert_not_called()
        self.assertEqual(gh.posted_comments, [])
        state = gh.pinned_data(VALIDATING_ISSUE)
        self.assertFalse(state.get(AWAITING_HUMAN))
        self.assertIsNone(state.get(PARK_REASON))
        # MUST NOT bump: nothing landed.
        self.assertEqual(state.get(REVIEW_ROUND), 1)

    def test_timeout_pushes_commits_and_bumps(self) -> None:
        # The dev committed the fix locally but the timeout killed it
        # before the push. Recovery must finish that push -- otherwise
        # the next tick's reviewer would inspect a SHA that is not on
        # the PR. `head_shas[0] != pre_dev_fix_sha` models "agent
        # produced a new commit before timing out."
        gh, issue = self._parked_issue(
            park_reason=AGENT_TIMEOUT,
            pre_dev_fix_sha=PRE_FIX_SHA,
        )

        with patch.object(workflow, WORKTREE_PATH, return_value=Path(WORKTREE_ROOT)):
            mocks = self._run_validating(
                gh,
                issue,
                run_agent=_agent(),
                dirty_files=(),
                push_branch=True,
                head_shas=("beef5678",),  # HEAD moved past pre-agent SHA
            )

        mocks[RUN_AGENT].assert_not_called()
        mocks[PUSH_BRANCH].assert_called_once()
        self.assertEqual(gh.posted_comments, [])
        state = gh.pinned_data(VALIDATING_ISSUE)
        self.assertFalse(state.get(AWAITING_HUMAN))
        self.assertIsNone(state.get(PARK_REASON))
        # Bumped: a real fix landed.
        self.assertEqual(state.get(REVIEW_ROUND), 2)
        self.assertIsNone(state.get("pre_dev_fix_sha"))
        # Stays on `validating` (no documenting hop) so the reviewer
        # re-evaluates the recovered head on the next tick.
        self.assertNotIn((VALIDATING_ISSUE, "documenting"), gh.label_history)

    def test_timeout_push_error_stays_parked(
        self,
    ) -> None:
        gh, issue = self._parked_issue(
            park_reason=AGENT_TIMEOUT,
            pre_dev_fix_sha=PRE_FIX_SHA,
        )

        with patch.object(workflow, WORKTREE_PATH, return_value=Path(WORKTREE_ROOT)):
            mocks = self._run_validating(
                gh,
                issue,
                run_agent=_agent(),
                dirty_files=(),
                push_branch=False,
                head_shas=("beef5678",),
            )

        mocks[PUSH_BRANCH].assert_called_once()
        self.assertEqual(gh.posted_comments, [])
        state = gh.pinned_data(VALIDATING_ISSUE)
        self.assertTrue(state.get(AWAITING_HUMAN))
        self.assertEqual(state.get(PARK_REASON), AGENT_TIMEOUT)
        # NOT bumped while still stuck; watermark preserved for next try.
        self.assertEqual(state.get(REVIEW_ROUND), 1)
        self.assertEqual(state.get("pre_dev_fix_sha"), PRE_FIX_SHA)


class ValidatingDevParkSafetyTest(
    unittest.TestCase,
    _TransientParkFixtureMixin,
):
    """Keep unsafe or unanchored dev timeout recoveries parked."""

    def test_dirty_timeout_stays_parked(self) -> None:
        # The dev edited files without committing before timing out.
        # Recovery refuses to silently push (would publish an incomplete
        # branch) or to clear flags (the next reviewer would inspect
        # uncommitted state). Stays parked until a human or comment-
        # driven resume sorts the dirty edits out.
        gh, issue = self._parked_issue(
            park_reason=AGENT_TIMEOUT,
            pre_dev_fix_sha=PRE_FIX_SHA,
        )

        with patch.object(workflow, WORKTREE_PATH, return_value=Path(WORKTREE_ROOT)):
            mocks = self._run_validating(
                gh,
                issue,
                run_agent=_agent(),
                dirty_files=["leftover.py"],
                push_branch=True,
            )

        mocks[RUN_AGENT].assert_not_called()
        mocks[PUSH_BRANCH].assert_not_called()
        # No new comment posted on this tick -- the original park
        # message still describes the situation.
        self.assertEqual(gh.posted_comments, [])
        state = gh.pinned_data(VALIDATING_ISSUE)
        self.assertTrue(state.get(AWAITING_HUMAN))
        self.assertEqual(state.get(PARK_REASON), AGENT_TIMEOUT)
        self.assertEqual(state.get(REVIEW_ROUND), 1)

    def test_timeout_without_watermark_stays_parked(self) -> None:
        # Defensive: if the timeout park ran in foreign code that did
        # not persist `pre_dev_fix_sha`, recovery cannot tell whether a
        # commit was produced. Refuse to act -- a force-push of a stale
        # local HEAD would silently rewrite remote.
        gh, issue = self._parked_issue(park_reason=AGENT_TIMEOUT)

        with patch.object(workflow, WORKTREE_PATH, return_value=Path(WORKTREE_ROOT)):
            mocks = self._run_validating(
                gh,
                issue,
                run_agent=_agent(),
                dirty_files=(),
                push_branch=True,
                head_shas=("anything",),
            )

        mocks[RUN_AGENT].assert_not_called()
        mocks[PUSH_BRANCH].assert_not_called()
        state = gh.pinned_data(VALIDATING_ISSUE)
        self.assertTrue(state.get(AWAITING_HUMAN))
        self.assertEqual(state.get(PARK_REASON), AGENT_TIMEOUT)

    def test_transient_comment_takes_resume_path(self) -> None:
        # A transient park is preempted by a fresh human comment: the
        # comment-driven resume path wins, the dev is spawned with the
        # human's feedback, and the recovery branch does not silently
        # retry the push. This ensures the human's reply is not dropped.
        gh, issue = self._parked_issue(park_reason=PUSH_FAILED)
        issue.comments.append(
            FakeComment(
                id=HUMAN_REPLY_ID,
                body=REBASE_REQUEST,
                user=FakeUser(HUMAN_LOGIN),
            )
        )

        with patch.object(workflow, WORKTREE_PATH, return_value=Path(WORKTREE_ROOT)):
            mocks = self._run_validating(
                gh,
                issue,
                run_agent=_agent(
                    session_id=DEV_SESSION,
                    last_message="rebased",
                ),
                push_branch=True,
                head_shas=["aaa", "bbb"],
            )

        # Dev was resumed with the human's feedback (recovery did NOT run).
        mocks[RUN_AGENT].assert_called_once()
        followup = mocks[RUN_AGENT].call_args.args[1]
        self.assertIn(REBASE_REQUEST, followup)
        state = gh.pinned_data(VALIDATING_ISSUE)
        self.assertFalse(state.get(AWAITING_HUMAN))


class HandleValidatingResumeOnHashChangeTest(
    unittest.TestCase,
    _PatchedWorkflowMixin,
):
    def test_body_drift_resumes_and_stays_validating(self) -> None:
        # While validating (PR is open), a human edit must not discard the
        # dev's already-pushed work. Notify and resume; on a successful
        # pushed fix, stay on `validating` so the reviewer re-evaluates
        # the new diff next tick. The docs pass only runs as the
        # final-docs handoff after a fresh approval.
        gh = FakeGitHubClient()
        issue = make_issue(BODY_DRIFT_ISSUE, label="validating", body="updated criteria")
        gh.add_issue(issue)
        pr = FakePR(number=BODY_DRIFT_PR, head_branch="orchestrator/geserdugarov__agent-orchestrator/issue-70")
        gh.add_pr(pr)
        gh.seed_state(
            BODY_DRIFT_ISSUE,
            user_content_hash="stale-hash",
            dev_agent="claude",
            dev_session_id=DEV_SESSION,
            pr_number=pr.number,
            review_round=0,
            branch="orchestrator/geserdugarov__agent-orchestrator/issue-70",
        )

        self._run_validating(
            gh,
            issue,
            run_agent=_agent(session_id=DEV_SESSION, last_message="fixed"),
            has_new_commits=True,
            dirty_files=(),
            push_branch=True,
            head_shas=["before-sha", "after-sha"],
        )

        # Stays on `validating`: no documenting hop, and the reviewer
        # has NOT been spawned this tick (the only run_agent call was
        # the dev resume).
        self.assertNotIn((BODY_DRIFT_ISSUE, "documenting"), gh.label_history)
        self.assertNotIn((BODY_DRIFT_ISSUE, "in_review"), gh.label_history)
        # Notice posted on the issue thread.
        self.assertTrue(
            any(
                "issue body changed" in body
                for _, body in gh.posted_comments
            )
        )
        # review_round incremented so the validating cap stays accurate.
        state = gh.pinned_data(BODY_DRIFT_ISSUE)
        self.assertEqual(state.get(REVIEW_ROUND), 1)


class ValidatingDriftDefersToReviewerRecoveryTest(
    unittest.TestCase,
    _PatchedWorkflowMixin,
):
    """Reviewer point 1: when validating is parked with a reviewer-side
    park reason (`reviewer_timeout` / `reviewer_failed`), a human "retry"
    comment must re-spawn the REVIEWER, not the dev session. The drift
    check fires first because the human's comment also flips the hash;
    the drift handler must defer to the awaiting-human branch in this
    case so the reviewer re-runs naturally."""

    def test_timeout_drift_respawns_reviewer(
        self,
    ) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(
            1000,
            label="validating",
            body="initial body",
        )
        # Pre-existing human "retry" comment that triggers the drift
        # detection (the hash includes non-orchestrator comments).
        human = FakeComment(
            id=REVIEWER_RETRY_COMMENT_ID,
            body="retry the reviewer please",
            user=FakeUser(HUMAN_LOGIN),
        )
        issue.comments.append(human)
        gh.add_issue(issue)
        pr = FakePR(number=REVIEWER_DRIFT_PR, head_branch="orchestrator/geserdugarov__agent-orchestrator/issue-1000")
        gh.add_pr(pr)
        # Pre-seed a real `user_content_hash` (the bug surfaces only
        # when the hash is already set; first-tick auto-seeding hides it).
        seed_hash = workflow._compute_user_content_hash(
            make_issue(1000, body="initial body"),
            set(),
        )
        gh.seed_state(
            1000,
            pr_number=pr.number,
            dev_agent="claude",
            dev_session_id=DEV_SESSION,
            review_round=1,
            branch="orchestrator/geserdugarov__agent-orchestrator/issue-1000",
            awaiting_human=True,
            park_reason="reviewer_timeout",
            last_action_comment_id=100,
            user_content_hash=seed_hash,
        )

        mocks = self._run_validating(
            gh,
            issue,
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
        call_args = mocks[RUN_AGENT].call_args
        self.assertEqual(call_args[0][0], config.REVIEW_AGENT)
        self.assertIn("automated code reviewer", call_args[0][1])
        # No drift-style ":pencil2: issue body changed; resuming dev
        # session" notice was posted -- the drift was deferred.
        self.assertFalse(
            any(
                ":pencil2:" in body
                and "resuming dev session" in body
                for _, body in gh.posted_comments
            )
        )
        # The reviewer recovery consumed the human comment and cleared
        # the park flags.
        state = gh.pinned_data(1000)
        self.assertFalse(state.get(AWAITING_HUMAN))
        self.assertIsNone(state.get(PARK_REASON))
        # The new hash baseline was persisted so the next tick doesn't
        # loop on the same drift.
        new_hash = workflow._compute_user_content_hash(issue, set())
        self.assertEqual(state.get("user_content_hash"), new_hash)
