# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import os
import unittest
from unittest.mock import MagicMock

os.environ.setdefault("ORCHESTRATOR_SKIP_DOTENV", "1")

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

    def test_docs_commit_pushed_advances_to_validating(self) -> None:
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
        self.assertIn((201, "validating"), gh.label_history)

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
        self.assertIn((201, "validating"), gh.label_history)
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
        self.assertIn((301, "validating"), gh.label_history)
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
        self.assertIn((401, "validating"), gh.label_history)
        data = gh.pinned_data(401)
        self.assertEqual(data.get("docs_verdict"), "updated")
        # The pre-park comment id was consumed by the resume.
        self.assertEqual(data.get("last_action_comment_id"), 2100)

    def test_human_reply_no_commit_does_not_advance(self) -> None:
        # The resume produces no new commit (the dev replied with a
        # clarification or the agent did nothing). We MUST NOT treat
        # the PR's pre-existing implementation HEAD as a "new docs
        # commit" and advance to validating -- that would push an
        # undocumented PR forward.
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
        # would advance to validating with the docs commit invisible
        # to the reviewer.
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
        self.assertIn((404, "validating"), gh.label_history)
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
        # NOT advance to validating -- the docs commit is still
        # local-only.
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
        self.assertIn((407, "validating"), gh.label_history)
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
    """`_handle_documenting` reacts to title/body edits by clearing
    any prior park and re-running the docs pass with the updated
    body. Drift is the unblock signal."""

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
        )
        defaults.update(state)
        gh.seed_state(701, **defaults)
        return gh, issue

    def test_body_edit_clears_park_and_runs_fresh_docs_pass(self) -> None:
        gh, issue = self._seeded(
            awaiting_human=True,
            park_reason="agent_question",
        )
        # Edit the body so the drift detector fires.
        issue.body = "updated body with new docs requirements"

        mocks = self._run(
            lambda: workflow._handle_documenting(gh, _TEST_SPEC, issue),
            run_agent=_agent(
                session_id="dev-sess",
                last_message="docs: addressed updated body",
            ),
            push_branch=True,
            head_shas=["aaa", "bbb"],
            branch_ahead_behind=(0, 0),
        )

        # The drift was acted on: a notice was posted, the agent ran
        # (NOT the awaiting-human-no-comments fast path), the push
        # happened, and the issue advanced to validating.
        mocks["run_agent"].assert_called_once()
        mocks["_push_branch"].assert_called_once()
        self.assertIn((701, "validating"), gh.label_history)
        self.assertTrue(any(
            "issue body changed" in body
            for _, body in gh.posted_comments
        ))
        data = gh.pinned_data(701)
        # Park flags cleared.
        self.assertFalse(data.get("awaiting_human"))
        self.assertIsNone(data.get("park_reason"))
        # Drift hash updated -- a second tick would not re-fire drift.
        self.assertNotEqual(
            data.get("user_content_hash"),
            "stale-hash-from-original-body",
        )

    def test_body_edit_without_prior_park_still_updates_hash(self) -> None:
        # An in-flight tick (not parked) sees a body edit: the docs
        # prompt embeds the new body, so the natural fresh-spawn
        # path picks it up. We still post the notice and persist the
        # new hash so the next tick has a stable baseline.
        gh, issue = self._seeded()
        issue.body = "in-flight body edit"

        self._run(
            lambda: workflow._handle_documenting(gh, _TEST_SPEC, issue),
            run_agent=_agent(
                session_id="dev-sess",
                last_message="Verified docs.\nDOCS: NO_CHANGE",
            ),
            push_branch=True,
            head_shas=["aaa", "aaa"],
            branch_ahead_behind=(0, 0),
        )

        # Hash updated; notice posted; no_change verdict honored.
        data = gh.pinned_data(701)
        self.assertNotEqual(
            data.get("user_content_hash"),
            "stale-hash-from-original-body",
        )
        self.assertTrue(any(
            "issue body changed" in body
            for _, body in gh.posted_comments
        ))
        self.assertEqual(data.get("docs_verdict"), "no_change")
        self.assertIn((701, "validating"), gh.label_history)

    def test_body_edit_with_recovered_commit_forces_fresh_docs_pass(
        self,
    ) -> None:
        # A prior tick committed docs and parked before pushing; on
        # this tick a body edit lands AND the worktree is still ahead
        # of remote (ahead=1). The recovery shortcut would normally
        # push the local commit and flip to validating, but that
        # commit was authored against the OLD body. The drift route
        # must force the docs agent to re-read the updated body
        # before any push.
        gh, issue = self._seeded(park_reason="push_failed")
        issue.body = "updated body after prior docs commit"

        mocks = self._run(
            lambda: workflow._handle_documenting(gh, _TEST_SPEC, issue),
            run_agent=_agent(
                session_id="dev-sess",
                # Agent emits NO_CHANGE; the no-change-with-ahead
                # branch pushes the existing commit and advances.
                last_message=(
                    "Reviewed updated body; the prior docs commit "
                    "still satisfies it.\nDOCS: NO_CHANGE"
                ),
            ),
            push_branch=True,
            # Same SHA before/after -- dev did not add another
            # commit. The branch is still ahead of remote (the
            # prior tick's docs commit).
            head_shas=["old-docs-sha", "old-docs-sha"],
            branch_ahead_behind=(1, 0),
        )

        # The docs prompt MUST have run -- this is the regression
        # case. The recovery shortcut would have skipped run_agent.
        mocks["run_agent"].assert_called_once()
        prompt = mocks["run_agent"].call_args.kwargs.get(
            "prompt"
        ) or mocks["run_agent"].call_args.args[1]
        self.assertIn(
            "updated body after prior docs commit", prompt,
        )
        # The "recovered docs" comment (which the shortcut would
        # have posted) must NOT appear -- we did NOT take the
        # shortcut. Instead the no-change-with-ahead path posts
        # the "after no-change confirmation" notice.
        recovered_shortcut_notices = [
            body for _, body in gh.posted_pr_comments
            if "pushed recovered docs commit(s)." in body
        ]
        self.assertEqual(recovered_shortcut_notices, [])

        # The existing commit IS pushed -- the dev confirmed it
        # satisfies the new body -- and the issue advances to
        # validating.
        mocks["_push_branch"].assert_called_once()
        self.assertIn((701, "validating"), gh.label_history)
        data = gh.pinned_data(701)
        self.assertEqual(data.get("docs_verdict"), "updated")
        self.assertTrue(any(
            "recovered docs commit(s) after no-change confirmation"
            in body
            for _, body in gh.posted_pr_comments
        ))

    def test_body_edit_with_recovered_commit_lets_dev_add_more(
        self,
    ) -> None:
        # Same setup as above, but this time the dev decides the
        # new body needs ADDITIONAL docs work and commits on top.
        # The unified commit branch pushes the combined state.
        gh, issue = self._seeded(park_reason="push_failed")
        issue.body = "updated body wants extra docs"

        mocks = self._run(
            lambda: workflow._handle_documenting(gh, _TEST_SPEC, issue),
            run_agent=_agent(
                session_id="dev-sess",
                last_message="docs: covered the new requirement",
            ),
            push_branch=True,
            # New commit on top of the recovered one.
            head_shas=["old-docs-sha", "new-docs-sha"],
            branch_ahead_behind=(1, 0),
        )

        mocks["run_agent"].assert_called_once()
        prompt = mocks["run_agent"].call_args.kwargs.get(
            "prompt"
        ) or mocks["run_agent"].call_args.args[1]
        self.assertIn("updated body wants extra docs", prompt)
        mocks["_push_branch"].assert_called_once()
        self.assertIn((701, "validating"), gh.label_history)
        data = gh.pinned_data(701)
        self.assertEqual(data.get("docs_verdict"), "updated")
        self.assertEqual(data.get("docs_checked_sha"), "new-docs-sha")


if __name__ == "__main__":
    unittest.main()
