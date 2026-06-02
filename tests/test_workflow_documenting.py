# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

os.environ.setdefault("ORCHESTRATOR_SKIP_DOTENV", "1")

from orchestrator import workflow

from tests.fakes import (
    FakeComment,
    FakeGitHubClient,
    FakeLabel,
    FakePR,
    FakePRRef,
    FakeUser,
    make_issue,
)
from tests.workflow_helpers import (
    _PatchedWorkflowMixin,
    _TEST_SPEC,
    _agent,
)


class HandleDocumentingMissingPrNumberTest(unittest.TestCase):
    """Without a pinned `pr_number` the handler cannot anchor on the
    dev's PR branch; park awaiting human and stay idempotent on repeat
    ticks."""

    def test_parks_with_missing_pr_number_reason(self) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(101, label="documenting")
        gh.add_issue(issue)

        workflow._handle_documenting(gh, _TEST_SPEC, issue)

        data = gh.pinned_data(101)
        self.assertTrue(data.get("awaiting_human"))
        self.assertIn("documenting", gh.posted_comments[-1][1])
        # Label is not flipped -- the operator decides whether to
        # relabel back or leave it.
        self.assertEqual(gh.label_history, [])

    def test_second_tick_already_parked_is_silent(self) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(102, label="documenting")
        gh.add_issue(issue)
        gh.seed_state(102, awaiting_human=True)

        workflow._handle_documenting(gh, _TEST_SPEC, issue)

        self.assertEqual(gh.posted_comments, [])
        self.assertEqual(gh.write_state_calls, 0)


class HandleDocumentingFreshRunTest(unittest.TestCase, _PatchedWorkflowMixin):
    """A docs agent run on a PR that already has commits."""

    def _seeded(self, **state):
        gh = FakeGitHubClient()
        issue = make_issue(201, label="documenting")
        gh.add_issue(issue)
        defaults = dict(
            pr_number=21,
            branch="orchestrator/issue-201",
            dev_agent="codex",
            dev_session_id="dev-sess",
        )
        defaults.update(state)
        gh.seed_state(201, **defaults)
        return gh, issue

    def test_docs_commit_pushed_advances_to_in_review(self) -> None:
        gh, issue = self._seeded()
        mocks = self._run(
            lambda: workflow._handle_documenting(gh, _TEST_SPEC, issue),
            run_agent=_agent(
                session_id="dev-sess",
                last_message="docs: updated README",
            ),
            push_branch=True,
            # before_sha + after_sha
            head_shas=["aaa", "bbb"],
            branch_ahead_behind=(0, 0),
        )

        self.assertEqual(mocks["run_agent"].call_count, 1)
        # The agent is spawned with the dev session id locked in.
        _, call_kwargs = mocks["run_agent"].call_args
        self.assertEqual(call_kwargs.get("resume_session_id"), "dev-sess")
        mocks["_push_branch"].assert_called_once()
        self.assertIn((201, "in_review"), gh.label_history)

    def test_agent_lifecycle_events_carry_review_round(self) -> None:
        # Documenting runs once per reviewer-approval handoff between
        # approval and `in_review`. The pinned `review_round` at the time
        # of approval (0 on the first approval, higher after fix loops)
        # must ride along on the spawn / exit audit events (and the
        # analytics record), so a downstream consumer can tell which
        # reviewer round the docs pass belonged to.
        gh, issue = self._seeded(review_round=2)
        self._run(
            lambda: workflow._handle_documenting(gh, _TEST_SPEC, issue),
            run_agent=_agent(
                session_id="dev-sess",
                last_message="docs: updated README",
            ),
            push_branch=True,
            head_shas=["aaa", "bbb"],
            branch_ahead_behind=(0, 0),
        )
        lifecycle = [
            e for e in gh.recorded_events
            if e["event"] in ("agent_spawn", "agent_exit")
            and e.get("stage") == "documenting"
        ]
        self.assertEqual(len(lifecycle), 2)
        for ev in lifecycle:
            self.assertEqual(ev.get("review_round"), 2)

        data = gh.pinned_data(201)
        self.assertEqual(data.get("docs_verdict"), "updated")
        self.assertEqual(data.get("docs_checked_sha"), "bbb")
        # A PR-conversation announcement is posted so reviewers see the
        # docs commit in context.
        self.assertTrue(any(
            ":books: documenting pass" in body
            for _, body in gh.posted_pr_comments
        ))

    def test_no_change_marker_advances_without_push(self) -> None:
        gh, issue = self._seeded()
        mocks = self._run(
            lambda: workflow._handle_documenting(gh, _TEST_SPEC, issue),
            run_agent=_agent(
                session_id="dev-sess",
                last_message=(
                    "Inspected diff; no user-facing change.\n"
                    "DOCS: NO_CHANGE"
                ),
            ),
            push_branch=True,
            # before + after both same -> no commit.
            head_shas=["aaa", "aaa"],
            branch_ahead_behind=(0, 0),
        )

        mocks["_push_branch"].assert_not_called()
        self.assertIn((201, "in_review"), gh.label_history)
        data = gh.pinned_data(201)
        self.assertEqual(data.get("docs_verdict"), "no_change")
        self.assertTrue(any(
            "no docs changes required" in body
            for _, body in gh.posted_pr_comments
        ))

    def test_no_commit_no_marker_parks_via_on_question(self) -> None:
        gh, issue = self._seeded()
        mocks = self._run(
            lambda: workflow._handle_documenting(gh, _TEST_SPEC, issue),
            run_agent=_agent(
                session_id="dev-sess",
                last_message="should I touch plans/roadmap.md too?",
            ),
            push_branch=True,
            head_shas=["aaa", "aaa"],
            branch_ahead_behind=(0, 0),
        )

        mocks["_push_branch"].assert_not_called()
        self.assertNotIn((201, "in_review"), gh.label_history)
        self.assertNotIn((201, "validating"), gh.label_history)
        data = gh.pinned_data(201)
        self.assertTrue(data.get("awaiting_human"))
        # The verdict is NOT recorded -- the agent did not give one.
        self.assertNotIn("docs_verdict", data)
        last_comment = gh.posted_comments[-1][1]
        self.assertIn("agent needs your input", last_comment)
        self.assertIn("plans/roadmap.md", last_comment)

    def test_silent_run_parks_as_agent_silent(self) -> None:
        # No commits, no message -- treat as a poisoned-session silent
        # crash like the implementing/validating handlers do.
        gh, issue = self._seeded()
        self._run(
            lambda: workflow._handle_documenting(gh, _TEST_SPEC, issue),
            run_agent=_agent(
                session_id="dev-sess", last_message="", exit_code=2,
            ),
            push_branch=True,
            head_shas=["aaa", "aaa"],
            branch_ahead_behind=(0, 0),
        )

        data = gh.pinned_data(201)
        self.assertTrue(data.get("awaiting_human"))
        self.assertEqual(data.get("park_reason"), "agent_silent")
        self.assertNotIn((201, "in_review"), gh.label_history)
        self.assertNotIn((201, "validating"), gh.label_history)

    def test_timeout_parks_with_agent_timeout(self) -> None:
        gh, issue = self._seeded()
        mocks = self._run(
            lambda: workflow._handle_documenting(gh, _TEST_SPEC, issue),
            run_agent=_agent(session_id="dev-sess", timed_out=True),
            push_branch=True,
            head_shas=["aaa"],
            branch_ahead_behind=(0, 0),
        )

        mocks["_push_branch"].assert_not_called()
        self.assertNotIn((201, "in_review"), gh.label_history)
        self.assertNotIn((201, "validating"), gh.label_history)
        data = gh.pinned_data(201)
        self.assertTrue(data.get("awaiting_human"))
        self.assertEqual(data.get("park_reason"), "agent_timeout")
        self.assertIn("agent timed out", gh.posted_comments[-1][1])

    def test_dirty_worktree_parks_without_push(self) -> None:
        gh, issue = self._seeded()
        mocks = self._run(
            lambda: workflow._handle_documenting(gh, _TEST_SPEC, issue),
            run_agent=_agent(
                session_id="dev-sess",
                last_message="docs: partial",
            ),
            push_branch=True,
            dirty_files=["README.md"],
            head_shas=["aaa", "bbb"],
            branch_ahead_behind=(0, 0),
        )

        mocks["_push_branch"].assert_not_called()
        self.assertNotIn((201, "in_review"), gh.label_history)
        self.assertNotIn((201, "validating"), gh.label_history)
        data = gh.pinned_data(201)
        self.assertTrue(data.get("awaiting_human"))
        # `_on_dirty_worktree` does NOT set a transient park_reason --
        # the worktree carries unreviewed edits and needs a human.
        last_comment = gh.posted_comments[-1][1]
        self.assertIn("uncommitted change", last_comment)
        self.assertIn("README.md", last_comment)

    def test_no_change_with_dirty_files_parks_as_dirty(self) -> None:
        # The agent edited files but did NOT commit, then emitted
        # `DOCS: NO_CHANGE`. Accepting that would advance to validating
        # while leaving uncommitted docs edits on disk -- the reviewer
        # agent (and any later push) would silently drop them. The
        # dirty check must run BEFORE the verdict parse.
        gh, issue = self._seeded()
        mocks = self._run(
            lambda: workflow._handle_documenting(gh, _TEST_SPEC, issue),
            run_agent=_agent(
                session_id="dev-sess",
                last_message="Tweaked README in place.\nDOCS: NO_CHANGE",
            ),
            push_branch=True,
            dirty_files=["README.md"],
            head_shas=["aaa", "aaa"],
            branch_ahead_behind=(0, 0),
        )

        mocks["_push_branch"].assert_not_called()
        self.assertNotIn((201, "in_review"), gh.label_history)
        self.assertNotIn((201, "validating"), gh.label_history)
        data = gh.pinned_data(201)
        self.assertTrue(data.get("awaiting_human"))
        self.assertNotIn("docs_verdict", data)
        last_comment = gh.posted_comments[-1][1]
        self.assertIn("uncommitted change", last_comment)
        self.assertIn("README.md", last_comment)

    def test_no_marker_with_dirty_files_parks_as_dirty(self) -> None:
        # Same shape as above but the agent ended with a question
        # instead of `DOCS: NO_CHANGE`. The dirty check must fire
        # before `_on_question`, otherwise an "agent needs your input"
        # park would silently abandon the uncommitted edits.
        gh, issue = self._seeded()
        mocks = self._run(
            lambda: workflow._handle_documenting(gh, _TEST_SPEC, issue),
            run_agent=_agent(
                session_id="dev-sess",
                last_message="What about plans/roadmap.md?",
            ),
            push_branch=True,
            dirty_files=["docs/architecture.md"],
            head_shas=["aaa", "aaa"],
            branch_ahead_behind=(0, 0),
        )

        mocks["_push_branch"].assert_not_called()
        self.assertNotIn((201, "in_review"), gh.label_history)
        self.assertNotIn((201, "validating"), gh.label_history)
        data = gh.pinned_data(201)
        self.assertTrue(data.get("awaiting_human"))
        last_comment = gh.posted_comments[-1][1]
        self.assertIn("uncommitted change", last_comment)
        self.assertIn("docs/architecture.md", last_comment)
        # The "agent needs your input" question park would be the
        # WRONG outcome here -- assert we did NOT take that path.
        self.assertNotIn("agent needs your input", last_comment)

    def test_silent_run_with_dirty_files_parks_as_dirty(self) -> None:
        # Empty final message AND dirty edits. Without the dirty
        # check, the silent-crash path (`_on_question` with
        # `agent_silent` reason) would fire and the dirty files
        # would be invisible to the operator.
        gh, issue = self._seeded()
        self._run(
            lambda: workflow._handle_documenting(gh, _TEST_SPEC, issue),
            run_agent=_agent(
                session_id="dev-sess", last_message="", exit_code=2,
            ),
            push_branch=True,
            dirty_files=["README.md"],
            head_shas=["aaa", "aaa"],
            branch_ahead_behind=(0, 0),
        )

        data = gh.pinned_data(201)
        self.assertTrue(data.get("awaiting_human"))
        self.assertNotIn((201, "in_review"), gh.label_history)
        self.assertNotIn((201, "validating"), gh.label_history)
        last_comment = gh.posted_comments[-1][1]
        self.assertIn("uncommitted change", last_comment)

    def test_behind_remote_parks_diverged_branch_before_spawn(self) -> None:
        # The local PR branch is behind `<remote>/<branch>` -- someone
        # force-pushed externally or a sibling-resolved-conflict
        # advanced the PR head. Pushing would clobber commits we
        # never saw, so refuse to spawn the agent at all.
        gh, issue = self._seeded()
        mocks = self._run(
            lambda: workflow._handle_documenting(gh, _TEST_SPEC, issue),
            run_agent=_agent(),
            push_branch=True,
            head_shas=[],
            branch_ahead_behind=(0, 2),
        )

        mocks["run_agent"].assert_not_called()
        mocks["_push_branch"].assert_not_called()
        self.assertNotIn((201, "in_review"), gh.label_history)
        self.assertNotIn((201, "validating"), gh.label_history)
        data = gh.pinned_data(201)
        self.assertTrue(data.get("awaiting_human"))
        self.assertEqual(data.get("park_reason"), "diverged_branch")
        last_comment = gh.posted_comments[-1][1]
        self.assertIn("behind", last_comment)

    def test_fetch_failure_parks_fetch_failed(self) -> None:
        # The PR-branch fetch fails (network / auth / branch deleted).
        # Without a current `<remote>/<branch>` we cannot reason about
        # ahead/behind, and a force-push under a stale lease could
        # clobber the real remote head. Park rather than guess.
        gh, issue = self._seeded()
        mocks = self._run(
            lambda: workflow._handle_documenting(gh, _TEST_SPEC, issue),
            run_agent=_agent(),
            push_branch=True,
            head_shas=[],
            branch_ahead_behind=(0, 0),
            authed_fetch_result=MagicMock(
                returncode=1, stdout="", stderr="fatal: ref not found",
            ),
        )

        mocks["run_agent"].assert_not_called()
        mocks["_push_branch"].assert_not_called()
        self.assertNotIn((201, "in_review"), gh.label_history)
        self.assertNotIn((201, "validating"), gh.label_history)
        data = gh.pinned_data(201)
        self.assertTrue(data.get("awaiting_human"))
        self.assertEqual(data.get("park_reason"), "fetch_failed")

    def test_push_failure_parks_with_push_failed(self) -> None:
        gh, issue = self._seeded()
        self._run(
            lambda: workflow._handle_documenting(gh, _TEST_SPEC, issue),
            run_agent=_agent(
                session_id="dev-sess",
                last_message="docs: README tweak",
            ),
            push_branch=False,
            head_shas=["aaa", "bbb"],
            branch_ahead_behind=(0, 0),
        )

        data = gh.pinned_data(201)
        self.assertTrue(data.get("awaiting_human"))
        self.assertEqual(data.get("park_reason"), "push_failed")
        self.assertNotIn((201, "in_review"), gh.label_history)
        self.assertNotIn((201, "validating"), gh.label_history)


class HandleDocumentingRecoveryTest(unittest.TestCase, _PatchedWorkflowMixin):
    """Restart recovery: a previous tick committed docs but crashed
    before the push lands."""

    def _seeded(self, **state):
        gh = FakeGitHubClient()
        issue = make_issue(301, label="documenting")
        gh.add_issue(issue)
        defaults = dict(
            pr_number=31,
            branch="orchestrator/issue-301",
            dev_agent="codex",
            dev_session_id="dev-sess",
        )
        defaults.update(state)
        gh.seed_state(301, **defaults)
        return gh, issue

    def test_unpushed_recovered_commits_push_without_agent_spawn(self) -> None:
        gh, issue = self._seeded()
        mocks = self._run(
            lambda: workflow._handle_documenting(gh, _TEST_SPEC, issue),
            run_agent=_agent(),
            push_branch=True,
            # _head_sha is called once to record docs_checked_sha after
            # the push.
            head_shas=["recovered-sha"],
            branch_ahead_behind=(1, 0),
        )

        # The agent must NOT be spawned -- the recovered commits are
        # enough to advance.
        mocks["run_agent"].assert_not_called()
        mocks["_push_branch"].assert_called_once()
        self.assertIn((301, "in_review"), gh.label_history)
        data = gh.pinned_data(301)
        self.assertEqual(data.get("docs_verdict"), "updated")
        self.assertEqual(data.get("docs_checked_sha"), "recovered-sha")
        self.assertTrue(any(
            "recovered docs commit" in body
            for _, body in gh.posted_pr_comments
        ))

    def test_recovery_push_failure_parks_push_failed(self) -> None:
        gh, issue = self._seeded()
        mocks = self._run(
            lambda: workflow._handle_documenting(gh, _TEST_SPEC, issue),
            run_agent=_agent(),
            push_branch=False,
            # The recovery branch falls through to the unified
            # commit/dirty/push block, which reads `after_sha`.
            head_shas=["recovered-sha"],
            branch_ahead_behind=(1, 0),
        )

        mocks["run_agent"].assert_not_called()
        self.assertNotIn((301, "in_review"), gh.label_history)
        self.assertNotIn((301, "validating"), gh.label_history)
        data = gh.pinned_data(301)
        self.assertTrue(data.get("awaiting_human"))
        self.assertEqual(data.get("park_reason"), "push_failed")

    def test_recovery_with_dirty_worktree_parks_without_push(self) -> None:
        # A previous tick committed docs AND left some files
        # uncommitted, then crashed. The recovery branch must NOT push:
        # the push would publish an incomplete branch (the dirty files
        # would silently disappear from what the reviewer agent sees).
        gh, issue = self._seeded()
        mocks = self._run(
            lambda: workflow._handle_documenting(gh, _TEST_SPEC, issue),
            run_agent=_agent(),
            push_branch=True,
            dirty_files=["docs/dirty.md"],
            head_shas=["recovered-sha"],
            branch_ahead_behind=(1, 0),
        )

        mocks["run_agent"].assert_not_called()
        mocks["_push_branch"].assert_not_called()
        self.assertNotIn((301, "in_review"), gh.label_history)
        self.assertNotIn((301, "validating"), gh.label_history)
        data = gh.pinned_data(301)
        self.assertTrue(data.get("awaiting_human"))
        last_comment = gh.posted_comments[-1][1]
        self.assertIn("uncommitted change", last_comment)
        self.assertIn("docs/dirty.md", last_comment)


class HandleDocumentingAwaitingHumanResumeTest(
    unittest.TestCase, _PatchedWorkflowMixin
):
    """Awaiting-human resume: a human reply re-runs the full
    documentation prompt (NOT the short human-reply followup that
    implementing/validating use). Documenting's stage instructions
    (`DOCS: NO_CHANGE` marker, what files to inspect, what to commit)
    are part of the prompt itself, so a resume that skips them would
    let a `fetch_failed` / `agent_timeout` / `agent_silent` retry
    advance via a stray no-change verdict without ever doing a real
    docs pass."""

    def test_human_reply_resumes_dev_and_advances_on_commit(self) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(401, label="documenting")
        issue.comments.append(
            FakeComment(id=2100, body="add a note about flag X",
                        user=FakeUser("alice"))
        )
        gh.add_issue(issue)
        gh.seed_state(
            401,
            pr_number=41,
            branch="orchestrator/issue-401",
            awaiting_human=True,
            last_action_comment_id=2000,
            dev_agent="codex",
            dev_session_id="dev-sess",
        )

        mocks = self._run(
            lambda: workflow._handle_documenting(gh, _TEST_SPEC, issue),
            run_agent=_agent(
                session_id="dev-sess",
                last_message="docs: flag X explained",
            ),
            push_branch=True,
            # The awaiting-human path captures `before_sha` from the PR
            # worktree BEFORE the resume, then reads `after_sha` post-
            # spawn. before_sha != after_sha means a docs commit
            # landed.
            head_shas=["aaa", "bbb"],
            branch_ahead_behind=(0, 0),
        )

        # The resumed run is the only agent spawn.
        self.assertEqual(mocks["run_agent"].call_count, 1)
        # The PR worktree is anchored BEFORE the resume helper runs so
        # the helper's `_ensure_worktree` fallback cannot restore the
        # per-issue branch from `<remote>/<base>` and lose the dev's
        # PR commits.
        mocks["_ensure_pr_worktree"].assert_called_once_with(_TEST_SPEC, 401)
        mocks["_push_branch"].assert_called_once()
        self.assertIn((401, "in_review"), gh.label_history)
        data = gh.pinned_data(401)
        self.assertEqual(data.get("docs_verdict"), "updated")
        # The pre-park comment id was consumed by the resume.
        self.assertEqual(data.get("last_action_comment_id"), 2100)

    def test_human_reply_no_commit_does_not_advance(self) -> None:
        # The resume produces no new commit (the dev replied with a
        # clarification or the agent did nothing). We MUST NOT treat
        # the PR's pre-existing implementation HEAD as a "new docs
        # commit" and advance -- that would push an undocumented PR
        # forward.
        gh = FakeGitHubClient()
        issue = make_issue(403, label="documenting")
        issue.comments.append(
            FakeComment(id=3100, body="why?", user=FakeUser("alice"))
        )
        gh.add_issue(issue)
        gh.seed_state(
            403,
            pr_number=43,
            branch="orchestrator/issue-403",
            awaiting_human=True,
            last_action_comment_id=3000,
            dev_agent="codex",
            dev_session_id="dev-sess",
            # NB: no `docs_checked_sha` -- the prior tick parked before
            # snapshotting one. The fix must capture a fresh
            # `before_sha` from the PR worktree at this tick.
        )

        mocks = self._run(
            lambda: workflow._handle_documenting(gh, _TEST_SPEC, issue),
            run_agent=_agent(
                session_id="dev-sess",
                last_message="should I also update README?",
            ),
            push_branch=True,
            # Same SHA before/after -- nothing new committed even
            # though HEAD is non-empty (the dev's implementation
            # commit).
            head_shas=["pr-head-sha", "pr-head-sha"],
            branch_ahead_behind=(0, 0),
        )

        mocks["_push_branch"].assert_not_called()
        self.assertNotIn((403, "in_review"), gh.label_history)
        self.assertNotIn((403, "validating"), gh.label_history)
        data = gh.pinned_data(403)
        # Still parked: no commit means the docs pass did not land
        # anything and the issue must stay awaiting human input.
        self.assertTrue(data.get("awaiting_human"))
        self.assertNotIn("docs_verdict", data)

    def test_human_reply_no_change_with_unpushed_commit_pushes(self) -> None:
        # A previous tick committed docs and then parked (push_failed
        # / agent_timeout / dirty) -- the worktree carries an unpushed
        # docs commit (ahead == 1). The human's retry resumes the dev
        # which returns DOCS: NO_CHANGE without committing further.
        # The handler MUST push the pre-existing local commit before
        # advancing: a NO_CHANGE verdict only certifies the local
        # tree, not the remote PR head. Without the push the issue
        # would advance with the docs commit invisible to AUTO_MERGE.
        gh = FakeGitHubClient()
        issue = make_issue(404, label="documenting")
        issue.comments.append(
            FakeComment(id=4100, body="try again", user=FakeUser("alice"))
        )
        gh.add_issue(issue)
        gh.seed_state(
            404,
            pr_number=44,
            branch="orchestrator/issue-404",
            awaiting_human=True,
            last_action_comment_id=4000,
            dev_agent="codex",
            dev_session_id="dev-sess",
            park_reason="push_failed",
        )

        mocks = self._run(
            lambda: workflow._handle_documenting(gh, _TEST_SPEC, issue),
            run_agent=_agent(
                session_id="dev-sess",
                last_message="No further docs needed.\nDOCS: NO_CHANGE",
            ),
            push_branch=True,
            # Same SHA before/after -- dev added nothing. The SHA
            # holds the prior tick's docs commit (which the remote
            # does not yet have).
            head_shas=["docs-sha", "docs-sha"],
            # ahead = 1 means the unpushed docs commit is still
            # waiting to land on the PR.
            branch_ahead_behind=(1, 0),
        )

        mocks["_push_branch"].assert_called_once()
        self.assertIn((404, "in_review"), gh.label_history)
        data = gh.pinned_data(404)
        self.assertEqual(data.get("docs_verdict"), "updated")
        self.assertEqual(data.get("docs_checked_sha"), "docs-sha")
        # The PR comment names the recovery-on-no-change path so a
        # reviewer scanning the PR can see why we advanced.
        self.assertTrue(any(
            "recovered docs commit" in body
            for _, body in gh.posted_pr_comments
        ))

    def test_human_reply_no_change_with_push_failure_parks(self) -> None:
        # Same shape as the previous test but the recovery push
        # itself fails. The issue must park with `push_failed` and
        # NOT advance -- the docs commit is still local-only.
        gh = FakeGitHubClient()
        issue = make_issue(405, label="documenting")
        issue.comments.append(
            FakeComment(id=5100, body="retry", user=FakeUser("alice"))
        )
        gh.add_issue(issue)
        gh.seed_state(
            405,
            pr_number=45,
            branch="orchestrator/issue-405",
            awaiting_human=True,
            last_action_comment_id=5000,
            dev_agent="codex",
            dev_session_id="dev-sess",
        )

        mocks = self._run(
            lambda: workflow._handle_documenting(gh, _TEST_SPEC, issue),
            run_agent=_agent(
                session_id="dev-sess",
                last_message="Reviewed; no change.\nDOCS: NO_CHANGE",
            ),
            push_branch=False,
            head_shas=["docs-sha", "docs-sha"],
            branch_ahead_behind=(1, 0),
        )

        mocks["_push_branch"].assert_called_once()
        self.assertNotIn((405, "in_review"), gh.label_history)
        self.assertNotIn((405, "validating"), gh.label_history)
        data = gh.pinned_data(405)
        self.assertTrue(data.get("awaiting_human"))
        self.assertEqual(data.get("park_reason"), "push_failed")

    def test_no_new_comments_keeps_parked(self) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(402, label="documenting")
        gh.add_issue(issue)
        gh.seed_state(
            402,
            pr_number=42,
            branch="orchestrator/issue-402",
            awaiting_human=True,
            last_action_comment_id=2500,
            dev_agent="codex",
            dev_session_id="dev-sess",
        )

        mocks = self._run(
            lambda: workflow._handle_documenting(gh, _TEST_SPEC, issue),
            run_agent=_agent(),
            push_branch=True,
            head_shas=["aaa"],
            branch_ahead_behind=(0, 0),
        )

        mocks["run_agent"].assert_not_called()
        mocks["_push_branch"].assert_not_called()
        self.assertNotIn((402, "in_review"), gh.label_history)
        self.assertNotIn((402, "validating"), gh.label_history)
        # Still parked; nothing changed.
        self.assertTrue(gh.pinned_data(402).get("awaiting_human"))

    def test_human_reply_resume_uses_full_documentation_prompt(self) -> None:
        # Regression: a `fetch_failed` / `agent_timeout` /
        # `agent_silent` resume cannot use the generic
        # `_resume_developer_on_human_reply` followup (which
        # contains ONLY the human's new comment text) -- the
        # documentation prompt's instructions
        # (DOCS: NO_CHANGE marker, files to inspect, what to
        # commit) must be reissued each resume. Otherwise the dev
        # could emit a stray no-change verdict learned from an
        # earlier spawn and advance without doing a real docs
        # pass.
        gh = FakeGitHubClient()
        issue = make_issue(
            406, label="documenting",
            body="implement helpful_function(x)",
        )
        issue.comments.append(
            FakeComment(id=6100, body="please retry", user=FakeUser("alice"))
        )
        gh.add_issue(issue)
        gh.seed_state(
            406,
            pr_number=46,
            branch="orchestrator/issue-406",
            awaiting_human=True,
            last_action_comment_id=6000,
            dev_agent="codex",
            dev_session_id="dev-sess",
            park_reason="agent_timeout",
        )

        mocks = self._run(
            lambda: workflow._handle_documenting(gh, _TEST_SPEC, issue),
            run_agent=_agent(
                session_id="dev-sess",
                last_message="docs: documented helpful_function",
            ),
            push_branch=True,
            head_shas=["aaa", "bbb"],
            branch_ahead_behind=(0, 0),
        )

        # The prompt MUST be the full docs prompt, not just the
        # human's "please retry" comment.
        prompt = (
            mocks["run_agent"].call_args.kwargs.get("prompt")
            or mocks["run_agent"].call_args.args[1]
        )
        # Hallmarks of `_build_documentation_prompt`:
        self.assertIn("documentation pass", prompt)
        self.assertIn("DOCS: NO_CHANGE", prompt)
        # The issue body is embedded so the dev re-reads the
        # current requirements.
        self.assertIn("implement helpful_function(x)", prompt)
        # The human's reply still surfaces (via the
        # recent-comments thread that the prompt embeds).
        self.assertIn("please retry", prompt)
        # Comment was consumed.
        data = gh.pinned_data(406)
        self.assertEqual(data.get("last_action_comment_id"), 6100)

    def test_human_reply_no_change_persists_docs_checked_sha(self) -> None:
        # Regression: a NO_CHANGE outcome on a resume (no prior
        # fresh-spawn ran on this issue this lifecycle) must
        # still persist `docs_checked_sha` to the SHA the dev
        # evaluated. Without it, a subsequent no-change retry
        # after a transient park (`fetch_failed`,
        # `diverged_branch`, timeout) would leave the watermark
        # unset and downstream consumers could not tell which
        # commit was verified.
        gh = FakeGitHubClient()
        issue = make_issue(407, label="documenting")
        issue.comments.append(
            FakeComment(id=7100, body="retry", user=FakeUser("alice"))
        )
        gh.add_issue(issue)
        gh.seed_state(
            407,
            pr_number=47,
            branch="orchestrator/issue-407",
            awaiting_human=True,
            last_action_comment_id=7000,
            dev_agent="codex",
            dev_session_id="dev-sess",
            park_reason="fetch_failed",
            # No docs_checked_sha seeded -- this is the first
            # successful no-change for this issue.
        )

        mocks = self._run(
            lambda: workflow._handle_documenting(gh, _TEST_SPEC, issue),
            run_agent=_agent(
                session_id="dev-sess",
                last_message="Reviewed; no change.\nDOCS: NO_CHANGE",
            ),
            push_branch=True,
            head_shas=["pr-head-sha", "pr-head-sha"],
            branch_ahead_behind=(0, 0),
        )

        # NO_CHANGE outcome on a remote-clean branch -- advance
        # without push and record the SHA the dev verified.
        mocks["_push_branch"].assert_not_called()
        self.assertIn((407, "in_review"), gh.label_history)
        data = gh.pinned_data(407)
        self.assertEqual(data.get("docs_verdict"), "no_change")
        self.assertEqual(data.get("docs_checked_sha"), "pr-head-sha")


class HandleDocumentingParkedSilenceTest(
    unittest.TestCase, _PatchedWorkflowMixin
):
    """Already-parked issues must not re-post the park comment on
    every poll. The fetch + behind branches in particular would
    otherwise spam the issue with `fetch_failed` / `diverged_branch`
    notices each tick while the operator drafts a reply."""

    def _seeded(self, **state):
        gh = FakeGitHubClient()
        issue = make_issue(601, label="documenting")
        gh.add_issue(issue)
        defaults = dict(
            pr_number=61,
            branch="orchestrator/issue-601",
            dev_agent="codex",
            dev_session_id="dev-sess",
            awaiting_human=True,
            last_action_comment_id=6000,
            # Seed the drift baseline so the drift detector is a
            # no-op for this test class -- otherwise its
            # first-encounter persistence would itself trip a
            # state write and confuse the "silent on re-tick"
            # assertions below.
            user_content_hash=workflow._compute_user_content_hash(
                issue, set(),
            ),
        )
        defaults.update(state)
        gh.seed_state(601, **defaults)
        return gh, issue

    def test_parked_with_no_new_comments_short_circuits_before_fetch(
        self,
    ) -> None:
        gh, issue = self._seeded(park_reason="agent_question")
        mocks = self._run(
            lambda: workflow._handle_documenting(gh, _TEST_SPEC, issue),
            run_agent=_agent(),
            push_branch=True,
            head_shas=[],
            branch_ahead_behind=(0, 0),
        )

        # No fetch, no agent spawn, no posted comments. The original
        # park is preserved verbatim.
        mocks["_authed_fetch"].assert_not_called()
        mocks["_ensure_pr_worktree"].assert_not_called()
        mocks["run_agent"].assert_not_called()
        self.assertEqual(gh.posted_comments, [])
        self.assertEqual(gh.posted_pr_comments, [])
        self.assertEqual(gh.write_state_calls, 0)

    def test_parked_with_no_new_comments_does_not_repark_on_fetch_fail(
        self,
    ) -> None:
        # If the fetch would have failed on this tick, the parked
        # issue must still stay silent -- the fetch call must not
        # even fire.
        gh, issue = self._seeded(park_reason="agent_question")
        mocks = self._run(
            lambda: workflow._handle_documenting(gh, _TEST_SPEC, issue),
            run_agent=_agent(),
            push_branch=True,
            head_shas=[],
            branch_ahead_behind=(0, 0),
            authed_fetch_result=MagicMock(
                returncode=1, stdout="", stderr="would-fail",
            ),
        )

        mocks["_authed_fetch"].assert_not_called()
        self.assertEqual(gh.posted_comments, [])
        # The original park reason survives untouched.
        self.assertEqual(
            gh.pinned_data(601).get("park_reason"), "agent_question",
        )

    def test_parked_with_no_new_comments_does_not_repark_on_diverged(
        self,
    ) -> None:
        # Same shape for a behind-remote tick.
        gh, issue = self._seeded(park_reason="dirty_worktree")
        mocks = self._run(
            lambda: workflow._handle_documenting(gh, _TEST_SPEC, issue),
            run_agent=_agent(),
            push_branch=True,
            head_shas=[],
            branch_ahead_behind=(0, 3),
        )

        mocks["_branch_ahead_behind"].assert_not_called()
        self.assertEqual(gh.posted_comments, [])
        # Park reason is preserved -- we did NOT clobber it with
        # `diverged_branch`.
        self.assertEqual(
            gh.pinned_data(601).get("park_reason"), "dirty_worktree",
        )


class HandleDocumentingDriftTest(
    unittest.TestCase, _PatchedWorkflowMixin
):
    """A user-content drift mid-final-docs-hop posts a notice and
    relabels back to `validating` for re-review -- no docs spawn,
    no push."""

    def _seeded(self, **state):
        gh = FakeGitHubClient()
        issue = make_issue(
            701, label="documenting", body="original body",
        )
        gh.add_issue(issue)
        defaults = dict(
            pr_number=71,
            branch="orchestrator/issue-701",
            dev_agent="codex",
            dev_session_id="dev-sess",
            user_content_hash="stale-hash-from-original-body",
            review_round=2,
        )
        defaults.update(state)
        gh.seed_state(701, **defaults)
        return gh, issue

    def test_body_edit_relabels_to_validating_without_spawn(self) -> None:
        # A body edit during the final-docs hop must reset
        # `review_round=0`, post the notice, and relabel to
        # `validating` so the reviewer re-evaluates on the next tick.
        # No docs agent runs.
        gh, issue = self._seeded(
            awaiting_human=True,
            park_reason="agent_question",
        )
        issue.body = "updated body with new docs requirements"

        mocks = self._run(
            lambda: workflow._handle_documenting(gh, _TEST_SPEC, issue),
            run_agent=_agent(),
            push_branch=True,
            head_shas=[],
            branch_ahead_behind=(0, 0),
        )

        # The drift case routes WITHOUT spawning the docs agent or
        # pushing -- a docs commit would just need to be re-reviewed
        # alongside any impl change.
        mocks["run_agent"].assert_not_called()
        mocks["_push_branch"].assert_not_called()
        self.assertIn((701, "validating"), gh.label_history)
        self.assertNotIn((701, "in_review"), gh.label_history)
        self.assertTrue(any(
            "issue body changed" in body
            for _, body in gh.posted_comments
        ))
        data = gh.pinned_data(701)
        # Park flags cleared.
        self.assertFalse(data.get("awaiting_human"))
        self.assertIsNone(data.get("park_reason"))
        self.assertEqual(data.get("review_round"), 0)
        # Drift hash updated -- a second tick would not re-fire drift.
        self.assertNotEqual(
            data.get("user_content_hash"),
            "stale-hash-from-original-body",
        )

    def test_body_edit_without_prior_park_relabels_to_validating(self) -> None:
        # An in-flight tick (not parked) sees a body edit: same drift
        # invalidation as the parked case -- relabel to `validating`,
        # no docs spawn.
        gh, issue = self._seeded()
        issue.body = "in-flight body edit"

        mocks = self._run(
            lambda: workflow._handle_documenting(gh, _TEST_SPEC, issue),
            run_agent=_agent(),
            push_branch=True,
            head_shas=[],
            branch_ahead_behind=(0, 0),
        )

        mocks["run_agent"].assert_not_called()
        mocks["_push_branch"].assert_not_called()
        # Hash updated; notice posted; relabel to validating.
        data = gh.pinned_data(701)
        self.assertNotEqual(
            data.get("user_content_hash"),
            "stale-hash-from-original-body",
        )
        self.assertTrue(any(
            "issue body changed" in body
            for _, body in gh.posted_comments
        ))
        self.assertIn((701, "validating"), gh.label_history)
        self.assertNotIn((701, "in_review"), gh.label_history)
        self.assertEqual(data.get("review_round"), 0)

    def test_body_edit_with_recovered_commit_still_relabels_without_push(
        self,
    ) -> None:
        # A prior final-docs tick committed docs and parked before
        # pushing; on this tick a body edit lands AND the worktree is
        # still ahead of remote (ahead=1). The recovered commit was
        # authored against the OLD body, so the handler MUST NOT push
        # it on this tick. Relabel to `validating`; the on-disk reset
        # is covered by `test_body_edit_resets_unpushed_local_docs_commit`
        # below (this test uses the default `_FAKE_WT` path that
        # doesn't exist, so the worktree-reset branch is a no-op here).
        gh, issue = self._seeded(park_reason="push_failed")
        issue.body = "updated body after prior docs commit"

        mocks = self._run(
            lambda: workflow._handle_documenting(gh, _TEST_SPEC, issue),
            run_agent=_agent(),
            push_branch=True,
            head_shas=[],
            branch_ahead_behind=(0, 0),
        )

        mocks["run_agent"].assert_not_called()
        mocks["_push_branch"].assert_not_called()
        self.assertIn((701, "validating"), gh.label_history)
        self.assertNotIn((701, "in_review"), gh.label_history)
        self.assertTrue(any(
            "issue body changed" in body
            for _, body in gh.posted_comments
        ))
        data = gh.pinned_data(701)
        self.assertEqual(data.get("review_round"), 0)

    def test_body_edit_resets_unpushed_local_docs_commit(self) -> None:
        # Regression: drift mid-final-docs-hop must discard any
        # unpushed local docs commit before relabeling to `validating`.
        # Otherwise the recovered-commit shortcut on a future
        # final-docs hop (driven by `ahead > 0` vs.
        # `<remote>/<branch>`) would push the stale commit -- authored
        # against the OLD body -- without spawning a fresh docs agent
        # against the new requirements. With `SQUASH_ON_APPROVAL=off`
        # this is particularly dangerous because the reviewer-approved
        # head is still the dev's PR head (no rewrite gap) so the
        # stale commit applies cleanly.
        gh, issue = self._seeded(park_reason="push_failed")
        issue.body = "updated body after prior docs commit"

        # `_git_hardened` is the inline probe + reset + clean surface.
        # Probe returns a parseable "behind\tahead" stdout with
        # ahead=1; reset and clean return success.
        probe_result = MagicMock(returncode=0, stdout="0\t1\n", stderr="")
        reset_result = MagicMock(returncode=0, stdout="", stderr="")
        clean_result = MagicMock(returncode=0, stdout="", stderr="")
        git_hardened_mock = MagicMock(
            side_effect=[probe_result, reset_result, clean_result],
        )

        with tempfile.TemporaryDirectory() as wt_dir:
            wt_path = Path(wt_dir)
            wt_path_mock = MagicMock(return_value=wt_path)
            with patch.object(workflow, "_worktree_path", wt_path_mock), \
                 patch.object(workflow, "_git_hardened", git_hardened_mock):
                mocks = self._run(
                    lambda: workflow._handle_documenting(
                        gh, _TEST_SPEC, issue,
                    ),
                    run_agent=_agent(),
                    push_branch=True,
                    head_shas=[],
                )

        # No docs agent ran; no push happened. Routed to validating.
        mocks["run_agent"].assert_not_called()
        mocks["_push_branch"].assert_not_called()
        self.assertIn((701, "validating"), gh.label_history)
        self.assertNotIn((701, "in_review"), gh.label_history)

        # Inline probe ran first, then reset, then clean.
        self.assertEqual(git_hardened_mock.call_count, 3)
        probe_call, reset_call, clean_call = (
            git_hardened_mock.call_args_list
        )
        self.assertEqual(probe_call.args[0], "rev-list")
        self.assertIn("--count", probe_call.args)
        self.assertEqual(probe_call.kwargs.get("cwd"), wt_path)
        self.assertEqual(reset_call.args[:2], ("reset", "--hard"))
        self.assertEqual(
            reset_call.args[2],
            f"{_TEST_SPEC.remote_name}/orchestrator/issue-701",
        )
        self.assertEqual(reset_call.kwargs.get("cwd"), wt_path)
        self.assertEqual(clean_call.args, ("clean", "-fd"))
        self.assertEqual(clean_call.kwargs.get("cwd"), wt_path)

        # Drift fetch was attempted before the probe + reset.
        mocks["_authed_fetch"].assert_called()

        data = gh.pinned_data(701)
        self.assertEqual(data.get("review_round"), 0)

    def test_body_edit_resets_dirty_worktree_with_no_local_commits(
        self,
    ) -> None:
        # Regression: a prior docs run may have edited files without
        # committing (parked via `_on_dirty_worktree` /
        # `_on_question` / `agent_timeout`) before the body edit
        # landed. Even when the local branch is in sync with remote
        # (`ahead == 0`), those uncommitted edits are docs work
        # against the OLD body and must be discarded before relabel.
        # The drift block must trigger `reset --hard` + `clean -fd`
        # on the dirty-only path.
        gh, issue = self._seeded(park_reason="dirty_worktree")
        issue.body = "updated body wants different docs"

        probe_result = MagicMock(returncode=0, stdout="0\t0\n", stderr="")
        reset_result = MagicMock(returncode=0, stdout="", stderr="")
        clean_result = MagicMock(returncode=0, stdout="", stderr="")
        git_hardened_mock = MagicMock(
            side_effect=[probe_result, reset_result, clean_result],
        )

        with tempfile.TemporaryDirectory() as wt_dir:
            wt_path = Path(wt_dir)
            wt_path_mock = MagicMock(return_value=wt_path)
            with patch.object(workflow, "_worktree_path", wt_path_mock), \
                 patch.object(workflow, "_git_hardened", git_hardened_mock):
                mocks = self._run(
                    lambda: workflow._handle_documenting(
                        gh, _TEST_SPEC, issue,
                    ),
                    run_agent=_agent(),
                    push_branch=True,
                    head_shas=[],
                    # Stale modified-tracked AND untracked paths from
                    # the prior dirty park.
                    dirty_files=[
                        "README.md",
                        "docs/new-section.md",
                    ],
                )

        # The dirty list was non-empty, so reset + clean fired even
        # though ahead == 0.
        self.assertEqual(git_hardened_mock.call_count, 3)
        probe_call, reset_call, clean_call = (
            git_hardened_mock.call_args_list
        )
        self.assertEqual(probe_call.args[0], "rev-list")
        self.assertEqual(reset_call.args[:2], ("reset", "--hard"))
        self.assertEqual(clean_call.args, ("clean", "-fd"))

        # Issue relabeled to validating, no agent run, no push.
        self.assertIn((701, "validating"), gh.label_history)
        self.assertNotIn((701, "in_review"), gh.label_history)
        mocks["run_agent"].assert_not_called()
        mocks["_push_branch"].assert_not_called()
        data = gh.pinned_data(701)
        self.assertEqual(data.get("review_round"), 0)

    def test_body_edit_resets_when_remote_advanced_past_local(
        self,
    ) -> None:
        # Regression: if the remote PR head advanced past local HEAD
        # while documenting was in flight (`behind > 0`) and then a
        # body edit fires drift, the handler must reset the worktree
        # to the freshly-fetched `<remote>/<branch>`. Without this,
        # the next reviewer round would `git diff` against the un-
        # fetched local HEAD and silently miss commits the remote
        # already has, breaking the "reviewer re-evaluates the
        # updated body against the current branch" contract.
        gh, issue = self._seeded()
        issue.body = "updated body with new docs requirements"

        # ahead=0, behind=2 ("remote moved past local").
        probe_result = MagicMock(returncode=0, stdout="2\t0\n", stderr="")
        reset_result = MagicMock(returncode=0, stdout="", stderr="")
        clean_result = MagicMock(returncode=0, stdout="", stderr="")
        git_hardened_mock = MagicMock(
            side_effect=[probe_result, reset_result, clean_result],
        )

        with tempfile.TemporaryDirectory() as wt_dir:
            wt_path = Path(wt_dir)
            wt_path_mock = MagicMock(return_value=wt_path)
            with patch.object(workflow, "_worktree_path", wt_path_mock), \
                 patch.object(workflow, "_git_hardened", git_hardened_mock):
                mocks = self._run(
                    lambda: workflow._handle_documenting(
                        gh, _TEST_SPEC, issue,
                    ),
                    run_agent=_agent(),
                    push_branch=True,
                    head_shas=[],
                )

        # Probe + reset + clean all fired -- the behind>0 case must
        # trigger the same reconcile shape as ahead>0 / dirty.
        self.assertEqual(git_hardened_mock.call_count, 3)
        probe_call, reset_call, clean_call = (
            git_hardened_mock.call_args_list
        )
        self.assertEqual(probe_call.args[0], "rev-list")
        self.assertEqual(reset_call.args[:2], ("reset", "--hard"))
        self.assertEqual(
            reset_call.args[2],
            f"{_TEST_SPEC.remote_name}/orchestrator/issue-701",
        )
        self.assertEqual(clean_call.args, ("clean", "-fd"))

        # Relabeled to validating; no agent / push.
        self.assertIn((701, "validating"), gh.label_history)
        self.assertNotIn((701, "in_review"), gh.label_history)
        mocks["run_agent"].assert_not_called()
        mocks["_push_branch"].assert_not_called()
        data = gh.pinned_data(701)
        self.assertEqual(data.get("review_round"), 0)

    def test_body_edit_parks_on_clean_failure(self) -> None:
        # Regression: `git clean -fd` is the final step of the drift
        # reconcile (after `reset --hard`) and removes untracked
        # files / directories that `reset --hard` does not touch. If
        # it fails, untracked docs edits authored against the OLD
        # body remain on disk; the next reviewer or docs run could
        # see them. Park with `worktree_reset_failed` rather than
        # relabeling.
        gh, issue = self._seeded(park_reason="push_failed")
        issue.body = "updated body after prior docs commit"

        probe_result = MagicMock(returncode=0, stdout="0\t1\n", stderr="")
        reset_result = MagicMock(returncode=0, stdout="", stderr="")
        clean_failure = MagicMock(
            returncode=128, stdout="",
            stderr="fatal: cannot remove path",
        )
        git_hardened_mock = MagicMock(
            side_effect=[probe_result, reset_result, clean_failure],
        )

        with tempfile.TemporaryDirectory() as wt_dir:
            wt_path = Path(wt_dir)
            wt_path_mock = MagicMock(return_value=wt_path)
            with patch.object(workflow, "_worktree_path", wt_path_mock), \
                 patch.object(workflow, "_git_hardened", git_hardened_mock):
                mocks = self._run(
                    lambda: workflow._handle_documenting(
                        gh, _TEST_SPEC, issue,
                    ),
                    run_agent=_agent(),
                    push_branch=True,
                    head_shas=[],
                )

        # All three calls fired: probe, reset, clean (which failed).
        self.assertEqual(git_hardened_mock.call_count, 3)
        clean_call = git_hardened_mock.call_args_list[-1]
        self.assertEqual(clean_call.args, ("clean", "-fd"))

        # Not relabeled; parked.
        self.assertNotIn((701, "validating"), gh.label_history)
        self.assertNotIn((701, "in_review"), gh.label_history)
        mocks["run_agent"].assert_not_called()
        mocks["_push_branch"].assert_not_called()
        data = gh.pinned_data(701)
        self.assertTrue(data.get("awaiting_human"))
        self.assertEqual(
            data.get("park_reason"), "worktree_reset_failed",
        )
        self.assertEqual(data.get("review_round"), 0)
        # Drift-unwind sentinel persists across the park so a later
        # retry tick re-attempts the cleanup + relabel.
        self.assertTrue(data.get("docs_drift_unwind_pending"))

    def test_body_edit_parks_on_ahead_behind_probe_failure(self) -> None:
        # Regression: `_branch_ahead_behind` swallows git errors as
        # `(0, 0)` ("in sync"), which would let a stale local docs
        # commit silently survive into the next final-docs hop's
        # recovered-commit shortcut. The drift block now probes
        # inline and parks with `worktree_reset_failed` when the
        # probe cannot be confirmed.
        gh, issue = self._seeded(park_reason="push_failed")
        issue.body = "updated body after prior docs commit"

        # Probe fails (rc=128 from a missing remote ref); reset must
        # NOT run because we can't trust the probe.
        probe_failure = MagicMock(
            returncode=128, stdout="", stderr="fatal: bad ref",
        )
        git_hardened_mock = MagicMock(return_value=probe_failure)

        with tempfile.TemporaryDirectory() as wt_dir:
            wt_path = Path(wt_dir)
            wt_path_mock = MagicMock(return_value=wt_path)
            with patch.object(workflow, "_worktree_path", wt_path_mock), \
                 patch.object(workflow, "_git_hardened", git_hardened_mock):
                mocks = self._run(
                    lambda: workflow._handle_documenting(
                        gh, _TEST_SPEC, issue,
                    ),
                    run_agent=_agent(),
                    push_branch=True,
                    head_shas=[],
                )

        # Only the probe ran; no reset attempted.
        self.assertEqual(git_hardened_mock.call_count, 1)
        self.assertEqual(
            git_hardened_mock.call_args.args[0], "rev-list",
        )

        # Not relabeled; parked.
        self.assertNotIn((701, "validating"), gh.label_history)
        self.assertNotIn((701, "in_review"), gh.label_history)
        mocks["run_agent"].assert_not_called()
        mocks["_push_branch"].assert_not_called()
        data = gh.pinned_data(701)
        self.assertTrue(data.get("awaiting_human"))
        self.assertEqual(
            data.get("park_reason"), "worktree_reset_failed",
        )
        self.assertEqual(data.get("review_round"), 0)
        # Drift-unwind sentinel persists across the park.
        self.assertTrue(data.get("docs_drift_unwind_pending"))

    def test_body_edit_parks_on_reset_failure(self) -> None:
        # Regression: the `git reset --hard <remote>/<branch>` is
        # rare-but-possible to fail (in-progress operation, fs
        # transient, etc.). If it fails, the stale local docs commit
        # is still on disk -- the next final-docs hop's recovered-
        # commit shortcut would push it. Park instead of relabeling.
        gh, issue = self._seeded(park_reason="push_failed")
        issue.body = "updated body after prior docs commit"

        probe_result = MagicMock(returncode=0, stdout="0\t1\n", stderr="")
        reset_failure = MagicMock(
            returncode=128, stdout="",
            stderr="fatal: rebase in progress",
        )
        git_hardened_mock = MagicMock(
            side_effect=[probe_result, reset_failure],
        )

        with tempfile.TemporaryDirectory() as wt_dir:
            wt_path = Path(wt_dir)
            wt_path_mock = MagicMock(return_value=wt_path)
            with patch.object(workflow, "_worktree_path", wt_path_mock), \
                 patch.object(workflow, "_git_hardened", git_hardened_mock):
                mocks = self._run(
                    lambda: workflow._handle_documenting(
                        gh, _TEST_SPEC, issue,
                    ),
                    run_agent=_agent(),
                    push_branch=True,
                    head_shas=[],
                )

        # Probe + reset both ran.
        self.assertEqual(git_hardened_mock.call_count, 2)
        probe_call, reset_call = git_hardened_mock.call_args_list
        self.assertEqual(probe_call.args[0], "rev-list")
        self.assertEqual(reset_call.args[:2], ("reset", "--hard"))

        # Not relabeled; parked.
        self.assertNotIn((701, "validating"), gh.label_history)
        self.assertNotIn((701, "in_review"), gh.label_history)
        mocks["run_agent"].assert_not_called()
        mocks["_push_branch"].assert_not_called()
        data = gh.pinned_data(701)
        self.assertTrue(data.get("awaiting_human"))
        self.assertEqual(
            data.get("park_reason"), "worktree_reset_failed",
        )
        self.assertEqual(data.get("review_round"), 0)
        # Drift-unwind sentinel persists so a later retry tick
        # re-attempts the cleanup + relabel without needing fresh
        # drift to trigger it.
        self.assertTrue(data.get("docs_drift_unwind_pending"))

    def test_pending_unwind_retries_cleanup_after_operator_unpark(
        self,
    ) -> None:
        # Regression for the operator-unpark gap: a prior tick's
        # drift unwind failed cleanup and parked, leaving the issue
        # on `documenting` with `docs_drift_unwind_pending=True`. If
        # the operator clears `awaiting_human` (manual unpark) and
        # the issue retains the marker, the next documenting tick
        # MUST retry the reconcile + relabel to `validating` -- not
        # fall through to the normal docs-spawn / recovered-commit
        # path, which would advance to `in_review` and skip the
        # required reviewer re-review of the edited body.
        gh, issue = self._seeded(
            # No `park_reason`: operator unparked.
            docs_drift_unwind_pending=True,
            user_content_hash=workflow._compute_user_content_hash(
                make_issue(
                    701, label="documenting", body="original body",
                ),
                set(),
            ),
        )
        # Refresh the seeded fixture's drift fields so the hash
        # detector returns None (no fresh drift this tick).
        issue.body = "original body"
        gh.seed_state(
            701,
            review_round=0,
            docs_drift_unwind_pending=True,
            user_content_hash=workflow._compute_user_content_hash(
                issue, set(),
            ),
            pr_number=71,
            branch="orchestrator/issue-701",
            dev_agent="codex",
            dev_session_id="dev-sess",
        )

        probe_result = MagicMock(returncode=0, stdout="0\t0\n", stderr="")
        git_hardened_mock = MagicMock(side_effect=[probe_result])

        with tempfile.TemporaryDirectory() as wt_dir:
            wt_path = Path(wt_dir)
            wt_path_mock = MagicMock(return_value=wt_path)
            with patch.object(workflow, "_worktree_path", wt_path_mock), \
                 patch.object(workflow, "_git_hardened", git_hardened_mock):
                mocks = self._run(
                    lambda: workflow._handle_documenting(
                        gh, _TEST_SPEC, issue,
                    ),
                    run_agent=_agent(),
                    push_branch=True,
                    head_shas=[],
                )

        # The retry path ran: probe fired, no reset needed (ahead=0,
        # behind=0, no dirty), relabeled to validating.
        mocks["_authed_fetch"].assert_called()
        self.assertEqual(git_hardened_mock.call_count, 1)
        self.assertEqual(
            git_hardened_mock.call_args.args[0], "rev-list",
        )
        # No agent run; no push.
        mocks["run_agent"].assert_not_called()
        mocks["_push_branch"].assert_not_called()
        # Relabeled to validating; marker cleared.
        self.assertIn((701, "validating"), gh.label_history)
        self.assertNotIn((701, "in_review"), gh.label_history)
        data = gh.pinned_data(701)
        self.assertFalse(data.get("docs_drift_unwind_pending"))

    def test_pending_unwind_silent_when_still_parked_no_input(
        self,
    ) -> None:
        # The drift-unwind retry MUST NOT fire on every tick while
        # the issue is parked with no new human input: that would
        # re-post the same park comment every tick and spam the
        # operator. The retry only re-engages when something has
        # changed (operator unpark OR fresh human comment).
        gh, issue = self._seeded(
            awaiting_human=True,
            park_reason="worktree_reset_failed",
            docs_drift_unwind_pending=True,
            last_action_comment_id=999,
            user_content_hash=workflow._compute_user_content_hash(
                make_issue(
                    701, label="documenting", body="original body",
                ),
                set(),
            ),
        )
        issue.body = "original body"
        gh.seed_state(
            701,
            review_round=0,
            docs_drift_unwind_pending=True,
            awaiting_human=True,
            park_reason="worktree_reset_failed",
            last_action_comment_id=999,
            user_content_hash=workflow._compute_user_content_hash(
                issue, set(),
            ),
            pr_number=71,
            branch="orchestrator/issue-701",
            dev_agent="codex",
            dev_session_id="dev-sess",
        )

        git_hardened_mock = MagicMock()
        with tempfile.TemporaryDirectory() as wt_dir:
            wt_path = Path(wt_dir)
            wt_path_mock = MagicMock(return_value=wt_path)
            with patch.object(workflow, "_worktree_path", wt_path_mock), \
                 patch.object(workflow, "_git_hardened", git_hardened_mock):
                mocks = self._run(
                    lambda: workflow._handle_documenting(
                        gh, _TEST_SPEC, issue,
                    ),
                    run_agent=_agent(),
                    push_branch=True,
                    head_shas=[],
                )

        # Silent: no fetch, no reset, no posted comments, no relabel.
        mocks["_authed_fetch"].assert_not_called()
        git_hardened_mock.assert_not_called()
        mocks["run_agent"].assert_not_called()
        mocks["_push_branch"].assert_not_called()
        self.assertEqual(gh.posted_comments, [])
        self.assertEqual(gh.posted_pr_comments, [])
        self.assertNotIn((701, "validating"), gh.label_history)
        self.assertNotIn((701, "in_review"), gh.label_history)
        # Marker preserved; the park is still in effect.
        data = gh.pinned_data(701)
        self.assertTrue(data.get("docs_drift_unwind_pending"))
        self.assertTrue(data.get("awaiting_human"))
        self.assertEqual(
            data.get("park_reason"), "worktree_reset_failed",
        )

    def test_body_edit_with_recovered_commit_parks_on_fetch_failure(
        self,
    ) -> None:
        # Regression: when the drift fetch fails AND the worktree
        # exists on disk, the handler cannot safely confirm whether
        # the local branch is ahead of remote. Park awaiting human
        # with `fetch_failed` rather than relabeling to `validating`
        # -- a stale local commit silently riding into the next
        # approval is worse than a park the operator can resolve.
        gh, issue = self._seeded(park_reason="push_failed")
        issue.body = "updated body after prior docs commit"

        with tempfile.TemporaryDirectory() as wt_dir:
            wt_path = Path(wt_dir)
            wt_path_mock = MagicMock(return_value=wt_path)
            git_hardened_mock = MagicMock()
            with patch.object(workflow, "_worktree_path", wt_path_mock), \
                 patch.object(workflow, "_git_hardened", git_hardened_mock):
                mocks = self._run(
                    lambda: workflow._handle_documenting(
                        gh, _TEST_SPEC, issue,
                    ),
                    run_agent=_agent(),
                    push_branch=True,
                    head_shas=[],
                    branch_ahead_behind=(1, 0),
                    authed_fetch_result=MagicMock(
                        returncode=1, stdout="", stderr="fetch boom",
                    ),
                )

        # No relabel; parked.
        self.assertNotIn((701, "validating"), gh.label_history)
        self.assertNotIn((701, "in_review"), gh.label_history)
        # No reset was attempted because the fetch failed.
        git_hardened_mock.assert_not_called()
        # No push, no agent.
        mocks["run_agent"].assert_not_called()
        mocks["_push_branch"].assert_not_called()
        data = gh.pinned_data(701)
        self.assertTrue(data.get("awaiting_human"))
        self.assertEqual(data.get("park_reason"), "fetch_failed")
        self.assertEqual(data.get("review_round"), 0)
        # Drift-unwind sentinel persists across the park.
        self.assertTrue(data.get("docs_drift_unwind_pending"))


class HandleDocumentingExternalMergeTest(
    unittest.TestCase, _PatchedWorkflowMixin
):
    """A human merged the PR before the docs pass ran. The handler must
    short-circuit to `done` without fetching the branch or spawning the
    docs agent.
    """

    def test_external_merge_finalizes_to_done(self) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(180, label="documenting")
        gh.add_issue(issue)
        pr = FakePR(
            number=18000,
            head_branch="orchestrator/issue-180",
            head=FakePRRef(sha="cafe1234"),
            merged=True,
            state="closed",
        )
        gh.add_pr(pr)
        gh.seed_state(
            180, pr_number=18000, branch="orchestrator/issue-180",
            dev_agent="claude", dev_session_id="dev-sess",
        )

        mocks = self._run(
            lambda: workflow._handle_documenting(gh, _TEST_SPEC, issue),
            run_agent=_agent(),
        )

        self.assertIn((180, "done"), gh.label_history)
        self.assertIn("merged_at", gh.pinned_data(180))
        self.assertTrue(issue.closed)
        mocks["run_agent"].assert_not_called()
        mocks["_cleanup_terminal_branch"].assert_called_once_with(
            gh, _TEST_SPEC, 180,
        )


class HandleDocumentingClosedIssueTest(
    unittest.TestCase, _PatchedWorkflowMixin
):
    """Closed `documenting` issues yielded by the new closed-issue sweep
    must NOT spawn the docs agent. The handler flips to `rejected`
    after the external-merge finalize returns False; the closed-PR-
    without-merge variant additionally runs branch cleanup.
    """

    def test_closed_documenting_with_closed_pr_runs_cleanup(self) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(181, label="documenting")
        issue.closed = True
        gh.add_issue(issue)
        pr = FakePR(
            number=18100,
            head_branch="orchestrator/issue-181",
            head=FakePRRef(sha="cafe1234"),
            merged=False,
            state="closed",
        )
        gh.add_pr(pr)
        gh.seed_state(
            181, pr_number=18100, branch="orchestrator/issue-181",
            dev_agent="claude", dev_session_id="dev-sess",
        )

        mocks = self._run(
            lambda: workflow._handle_documenting(gh, _TEST_SPEC, issue),
            run_agent=_agent(),
        )

        self.assertIn((181, "rejected"), gh.label_history)
        self.assertIn("closed_without_merge_at", gh.pinned_data(181))
        mocks["run_agent"].assert_not_called()
        mocks["_cleanup_terminal_branch"].assert_called_once_with(
            gh, _TEST_SPEC, 181,
        )


class HandleDocumentingFinalDocsHandoffTest(
    unittest.TestCase, _PatchedWorkflowMixin
):
    """Issue #266: when `_handle_validating` approves and relabels to
    `documenting`, the next `_handle_documenting` tick must advance to
    `in_review` (NOT back to `validating`) on every success exit.
    """

    PR_NUMBER = 71
    BRANCH = "orchestrator/issue-707"

    def _seeded(self, **state):
        gh = FakeGitHubClient()
        issue = make_issue(707, label="documenting")
        gh.add_issue(issue)
        defaults = dict(
            pr_number=self.PR_NUMBER,
            branch=self.BRANCH,
            dev_agent="codex",
            dev_session_id="dev-sess",
            review_round=2,
            pr_last_comment_id=999,
        )
        defaults.update(state)
        gh.seed_state(707, **defaults)
        return gh, issue

    def test_no_change_verdict_advances_to_in_review(
        self,
    ) -> None:
        gh, issue = self._seeded()
        mocks = self._run(
            lambda: workflow._handle_documenting(gh, _TEST_SPEC, issue),
            run_agent=_agent(
                session_id="dev-sess",
                last_message=(
                    "Inspected diff; no user-facing change.\n"
                    "DOCS: NO_CHANGE"
                ),
            ),
            push_branch=True,
            # No commit landed: before_sha == after_sha == approved head.
            head_shas=["approvedSha", "approvedSha"],
            branch_ahead_behind=(0, 0),
        )

        mocks["_push_branch"].assert_not_called()
        self.assertIn((707, "in_review"), gh.label_history)
        self.assertNotIn((707, "validating"), gh.label_history)
        data = gh.pinned_data(707)
        self.assertEqual(data.get("docs_verdict"), "no_change")

    def test_no_change_with_recovered_ahead_advances_to_in_review(
        self,
    ) -> None:
        # A previous final-docs tick committed but parked before the
        # push landed. The resume's no-change verdict triggers the
        # ahead-push branch; the recovered commit is now the new PR
        # head.
        gh, issue = self._seeded(awaiting_human=True, park_reason="push_failed")
        issue.comments.append(
            FakeComment(id=2000, body="retry please", user=FakeUser("alice")),
        )

        mocks = self._run(
            lambda: workflow._handle_documenting(gh, _TEST_SPEC, issue),
            run_agent=_agent(
                session_id="dev-sess",
                last_message=(
                    "Re-checked diff; the existing docs commit "
                    "already covers it.\nDOCS: NO_CHANGE"
                ),
            ),
            push_branch=True,
            # before_sha (awaiting-human resume snapshot) == after_sha
            # (no new commit), but ahead=1 (the recovered docs commit
            # from a prior tick) -- the helper pushes it and routes.
            head_shas=["recoveredDocsSha", "recoveredDocsSha"],
            branch_ahead_behind=(1, 0),
        )

        mocks["_push_branch"].assert_called_once()
        self.assertIn((707, "in_review"), gh.label_history)

    def test_user_content_drift_relabels_to_validating_without_spawn(
        self,
    ) -> None:
        # A human body edit during the final-docs hop must reset
        # `review_round=0`, post the notice, and relabel to
        # `validating` so the reviewer re-evaluates on the next tick
        # -- WITHOUT spawning the docs agent (a docs commit against
        # the old body would just need to be re-reviewed alongside
        # any impl change).
        gh, issue = self._seeded(user_content_hash="oldhash")

        mocks = self._run(
            lambda: workflow._handle_documenting(gh, _TEST_SPEC, issue),
            run_agent=_agent(),
            push_branch=True,
            head_shas=[],
            branch_ahead_behind=(0, 0),
        )

        mocks["run_agent"].assert_not_called()
        mocks["_push_branch"].assert_not_called()
        # Drift posted the issue-thread notice.
        self.assertTrue(any(
            "issue body changed" in body
            for _, body in gh.posted_comments
        ))
        # Route back through `validating`.
        self.assertIn((707, "validating"), gh.label_history)
        self.assertNotIn((707, "in_review"), gh.label_history)
        data = gh.pinned_data(707)
        self.assertEqual(data.get("review_round"), 0)

    def test_resume_consumed_reply_does_not_replay_as_in_review_feedback(
        self,
    ) -> None:
        # Lifecycle: validating approves at SHA `approvedSha` and seeds
        # `pr_last_comment_id=900` (past its pickup / PR-opened /
        # approval orchestrator comments). The first documenting tick
        # asks a question and parks at id=950 (orchestrator park
        # comment). A human replies at id=1100 on the issue thread.
        # The next documenting tick's awaiting-human resume consumes
        # 1100 (advances `last_action_comment_id=1100`), the dev
        # produces a docs commit, the helper pushes and relabels to
        # `in_review`. Without the watermark ratchet,
        # `pr_last_comment_id` stays at 900, and the next in_review
        # tick scans `comments_after(900)`, sees 1100 as fresh PR
        # feedback, and bounces to `fixing` over work the docs pass
        # already addressed.
        gh = FakeGitHubClient()
        long_ago = datetime.now(timezone.utc) - timedelta(hours=1)
        issue = make_issue(709, label="documenting", comments=[
            FakeComment(
                id=900, body=":robot: orchestrator picking this up.",
                user=FakeUser("orchestrator"), created_at=long_ago,
            ),
            FakeComment(
                id=950, body=":sos: agent needs your input to proceed",
                user=FakeUser("orchestrator"), created_at=long_ago,
            ),
            FakeComment(
                id=1100, body="please cover edge case X in README",
                user=FakeUser("alice"), created_at=long_ago,
            ),
        ])
        gh.add_issue(issue)
        pr = FakePR(
            number=73,
            head_branch="orchestrator/issue-709",
            head=FakePRRef(sha="docsSha"),
            mergeable=True, check_state="success",
        )
        gh.add_pr(pr)
        gh.seed_state(
            709,
            pr_number=73,
            branch="orchestrator/issue-709",
            dev_agent="codex",
            dev_session_id="dev-sess",
            review_round=1,
            pr_last_comment_id=900,
            pickup_comment_id=900,
            orchestrator_comment_ids=[900, 950],
            awaiting_human=True,
            park_reason="agent_question",
            last_action_comment_id=950,
        )

        # Documenting tick: awaiting-human resume consumes id=1100,
        # docs commit lands, advance to in_review.
        self._run(
            lambda: workflow._handle_documenting(gh, _TEST_SPEC, issue),
            run_agent=_agent(
                session_id="dev-sess",
                last_message="docs: cover edge case X",
            ),
            push_branch=True,
            head_shas=["approvedSha", "docsSha"],
            branch_ahead_behind=(0, 0),
        )

        self.assertIn((709, "in_review"), gh.label_history)
        data = gh.pinned_data(709)
        self.assertEqual(data.get("last_action_comment_id"), 1100)
        self.assertGreaterEqual(
            data.get("pr_last_comment_id"), 1100,
            "pr_last_comment_id must ratchet past the consumed human "
            "issue-thread reply on the final-docs handoff so the next "
            "in_review tick does not replay it as fresh PR feedback",
        )

        # In_review tick: ensure the consumed reply is NOT replayed as
        # fresh feedback (the actual route-to-fixing bug the ratchet
        # guards against).
        if not any(l.name == "in_review" for l in issue.labels):
            issue.labels = [FakeLabel("in_review")]
        mocks_ir = self._run(
            lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
            run_agent=_agent(),
        )

        mocks_ir["run_agent"].assert_not_called()
        self.assertNotIn(
            (709, "fixing"), gh.label_history,
            "in_review must not bounce to `fixing` over a human reply "
            "the documenting awaiting-human resume already consumed",
        )


if __name__ == "__main__":
    unittest.main()
